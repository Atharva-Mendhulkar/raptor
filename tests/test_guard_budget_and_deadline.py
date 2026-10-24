"""
Tests for Requirements G7, G8, G9, and G10:
- G7: UNKNOWN resolution via status check tool.
- G8: Token bucket retry budget.
- G9: Absolute deadline propagation and rejection.
- G10: /stats telemetry endpoint.
"""
import asyncio
import json
import time
import httpx
import pytest
import uvicorn
from starlette.requests import Request
from starlette.responses import Response

from raptor.common.models import LedgerState, ToolPolicy
from raptor.guard.budget import TokenBucketRetryBudget, check_deadline
from raptor.guard.ledger import GuardLedger
from raptor.guard.policy import GuardPolicyConfig, ToolPolicyDefinition
from raptor.guard.proxy import GuardProxyApp
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server


def test_token_bucket_retry_budget_accrual_and_exhaustion():
    """Requirement G8: Tokens accrue with first attempts, spend on retries, exhaust when empty."""
    budget = TokenBucketRetryBudget(token_ratio=0.5, burst=2.0, max_tokens=10.0)
    assert budget.tokens == 2.0
    assert budget.can_retry() is True

    # Spend 1st retry
    assert budget.consume_retry() is True
    assert budget.tokens == 1.0

    # Spend 2nd retry
    assert budget.consume_retry() is True
    assert budget.tokens == 0.0

    # Spend 3rd retry (exhausted!)
    assert budget.can_retry() is False
    assert budget.consume_retry() is False

    # First attempt accrues tokens
    budget.record_first_attempt()
    assert budget.tokens == 0.5
    assert budget.consume_retry() is False

    budget.record_first_attempt()
    assert budget.tokens == 1.0
    assert budget.consume_retry() is True


def test_deadline_check_rejection():
    """Requirement G9: Absolute deadline check rejects if remaining < min_service_time."""
    now = 1000.0
    # Deadline far in future
    valid, rem = check_deadline(1005.0, min_service_time=0.05, now=now)
    assert valid is True
    assert rem == 5.0

    # Deadline within min_service_time (40ms left when 50ms needed)
    valid, rem = check_deadline(1000.04, min_service_time=0.05, now=now)
    assert valid is False
    assert rem < 0.05

    # Past deadline
    valid, rem = check_deadline(999.0, min_service_time=0.05, now=now)
    assert valid is False


@pytest.mark.asyncio
async def test_guard_proxy_deadline_rejection():
    """Requirement G9: Guard returns DEADLINE_EXCEEDED when deadline remaining is too short."""
    policy_config = GuardPolicyConfig(
        tools={
            "transfer": ToolPolicyDefinition(
                effectful=True,
                idempotency_fields=["recipient", "amount"],
                min_service_time_seconds=0.10,
            )
        }
    )
    guard = GuardProxyApp(
        upstream_url="http://127.0.0.1:9999",
        policy_config=policy_config,
    )

    asgi = guard.as_asgi_app()
    transport = httpx.ASGITransport(app=asgi)

    async with httpx.AsyncClient(transport=transport, base_url="http://guard") as client:
        # Call with expired deadline
        payload = {
            "jsonrpc": "2.0",
            "id": "deadline_req_1",
            "method": "tools/call",
            "params": {
                "name": "transfer",
                "arguments": {
                    "amount": 100.0,
                    "recipient": "Alice",
                    "memo": "tx_deadline_fail",
                    "_meta": {"deadline_epoch": time.time() - 1.0},
                },
            },
        }
        resp = await client.post("/mcp", json=payload)
        assert resp.status_code == 200
        assert resp.headers.get("x-raptor-deadline") == "EXCEEDED"
        data = resp.json()
        assert data["result"]["isError"] is True
        assert data["result"]["error_code"] == "DEADLINE_EXCEEDED"
        assert guard.stats["deadline_exceeded"] == 1


@pytest.mark.asyncio
async def test_guard_proxy_budget_exhaustion():
    """Requirement G8: Guard returns BUDGET_EXHAUSTED when retry tokens are depleted."""
    budget = TokenBucketRetryBudget(token_ratio=0.0, burst=1.0)
    policy_config = GuardPolicyConfig(
        tools={
            "transfer": ToolPolicyDefinition(
                effectful=True,
                idempotency_fields=["recipient", "amount"],
            )
        }
    )
    guard = GuardProxyApp(
        upstream_url="http://127.0.0.1:9999",
        policy_config=policy_config,
        budget=budget,
    )

    asgi = guard.as_asgi_app()
    transport = httpx.ASGITransport(app=asgi)

    async with httpx.AsyncClient(transport=transport, base_url="http://guard") as client:
        # 1st attempt: spends 1 retry token because attempt=2
        payload = {
            "jsonrpc": "2.0",
            "id": "retry_1",
            "method": "tools/call",
            "params": {
                "name": "transfer",
                "arguments": {
                    "amount": 100.0,
                    "recipient": "Bob",
                    "memo": "tx_budget_1",
                    "_meta": {"attempt": 2},
                },
            },
        }
        # First retry allowed (budget starts with 1 token)
        # Upstream will fail connection since 9999 is dead, which is fine
        await client.post("/mcp", json=payload)

        # 2nd retry: token budget now 0 -> immediately BUDGET_EXHAUSTED
        payload["id"] = "retry_2"
        resp2 = await client.post("/mcp", json=payload)
        assert resp2.status_code == 200
        assert resp2.headers.get("x-raptor-budget") == "EXHAUSTED"
        data = resp2.json()
        assert data["result"]["isError"] is True
        assert data["result"]["error_code"] == "BUDGET_EXHAUSTED"
        assert guard.stats["budget_exhausted"] == 1


@pytest.mark.asyncio
async def test_guard_unknown_resolution_via_status_check():
    """
    Requirement G7: Resolve UNKNOWN outcome by executing policy status_check_tool.
    Simulates:
    1. Effect committed on backend server.
    2. Guard ledger marked UNKNOWN (simulating network timeout after commit).
    3. Retry request arrives.
    4. Guard automatically queries get_transfer_status, detects commit, updates ledger to COMMITTED, and replays!
    """
    server_port = 8921
    guard_port = 8922

    # 1. Upstream Tool Server
    backend_ledger = LedgerDB()
    server, _ = create_mock_mcp_server(ledger=backend_ledger)
    server_app = server.streamable_http_app()
    srv = uvicorn.Server(uvicorn.Config(server_app, host="127.0.0.1", port=server_port, log_level="error"))
    srv_task = asyncio.create_task(srv.serve())

    # 2. Guard Proxy with status_check_tool configured
    policy_config = GuardPolicyConfig.from_yaml_file("raptor/policies/guard_policy.yaml")
    guard_ledger = GuardLedger()
    guard = GuardProxyApp(
        upstream_url=f"http://127.0.0.1:{server_port}",
        policy_config=policy_config,
        ledger=guard_ledger,
    )
    guard_srv = uvicorn.Server(uvicorn.Config(guard.as_asgi_app(), host="127.0.0.1", port=guard_port, log_level="error"))
    guard_task = asyncio.create_task(guard_srv.serve())

    await asyncio.sleep(0.35)

    try:
        # Pre-record committed effect in backend ledger
        backend_ledger.record_effect(
            tool_name="transfer",
            logical_op_id="memo_g7_123",
            recipient="Charlie",
            amount=75.0,
        )

        from raptor.policies.transfer_policy import KeyDerivationEngine
        policy = policy_config.get_tool_policy("transfer")
        key, _ = KeyDerivationEngine(policy).derive_key(
            {"recipient": "Charlie", "amount": 75.0}
        )
        # Simulate previous network drop leaving key in UNKNOWN
        guard_ledger.mark_unknown(key)
        assert guard_ledger.get_entry(key).state == LedgerState.UNKNOWN

        async with httpx.AsyncClient(timeout=5.0) as client:
            req_payload = {
                "jsonrpc": "2.0",
                "id": "retry_after_unknown",
                "method": "tools/call",
                "params": {
                    "name": "transfer",
                    "arguments": {
                        "amount": 75.0,
                        "recipient": "Charlie",
                        "memo": "memo_g7_123",
                    },
                },
            }
            resp = await client.post(f"http://127.0.0.1:{guard_port}/mcp", json=req_payload)
            assert resp.status_code == 200
            assert resp.headers.get("x-raptor-dedup") == "RESOLVED_COMMITTED"

            entry = guard_ledger.get_entry(key)
            assert entry.state == LedgerState.COMMITTED
            assert guard.stats["unknown_resolved_committed"] == 1

            # Requirement G10: Telemetry stats
            stats_resp = await client.get(f"http://127.0.0.1:{guard_port}/stats")
            assert stats_resp.status_code == 200
            stats = stats_resp.json()
            assert stats["unknown_resolved_committed"] == 1

    finally:
        await guard.close()
        srv.should_exit = True
        guard_srv.should_exit = True
        await asyncio.gather(srv_task, guard_task, return_exceptions=True)

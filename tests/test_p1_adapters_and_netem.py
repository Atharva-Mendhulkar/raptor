"""
Tests for Requirements H3, H6 (A2A), H7 (W2 Model Agent), and Baseline B4:
- A2A SendMessage client and guard proxy deduplication.
- W2 Model Agent Adapter deterministic temperature-0 execution.
- B4 Durable Execution Activity Idempotency Adapter.
- H3 NetemController safe execution and fallback.
"""
import asyncio
from contextlib import asynccontextmanager
import pytest
import uvicorn

from raptor.clients.a2a_adapter import A2AClientAdapter
from raptor.clients.durable_adapter import DurableExecutionAdapter
from raptor.clients.model_agent import ModelAgentAdapter
from raptor.guard.ledger import GuardLedger
from raptor.guard.policy import GuardPolicyConfig
from raptor.guard.proxy import GuardProxyApp
from raptor.harness.netem import NetemController
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server


@asynccontextmanager
async def spawn_server_and_guard(server_port: int, guard_port: int):
    ledger = LedgerDB()
    server, _ = create_mock_mcp_server(ledger=ledger)
    server_app = server.streamable_http_app()

    srv = uvicorn.Server(uvicorn.Config(server_app, host="127.0.0.1", port=server_port, log_level="error"))
    srv_task = asyncio.create_task(srv.serve())

    guard_ledger = GuardLedger()
    policy = GuardPolicyConfig.from_yaml_file("raptor/policies/guard_policy.yaml")
    guard = GuardProxyApp(
        upstream_url=f"http://127.0.0.1:{server_port}",
        policy_config=policy,
        ledger=guard_ledger,
    )
    guard_srv = uvicorn.Server(uvicorn.Config(guard.as_asgi_app(), host="127.0.0.1", port=guard_port, log_level="error"))
    guard_task = asyncio.create_task(guard_srv.serve())

    await asyncio.sleep(0.35)
    try:
        yield {
            "server_port": server_port,
            "guard_port": guard_port,
            "backend_ledger": ledger,
            "guard_ledger": guard_ledger,
            "guard": guard,
        }
    finally:
        await guard.close()
        srv.should_exit = True
        guard_srv.should_exit = True
        await asyncio.gather(srv_task, guard_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a2a_adapter_and_guard_dedup():
    """Requirement H6 & G1: A2A SendMessage adapter and guard deduplication by messageId."""
    async with spawn_server_and_guard(server_port=8931, guard_port=8932) as ctx:
        guard_a2a_url = f"http://127.0.0.1:{ctx['guard_port']}/a2a"
        adapter = A2AClientAdapter(endpoint_url=guard_a2a_url, max_retries=1)

        # 1. First execution
        res1 = await adapter.execute_transfer(
            amount=60.0,
            recipient="A2ATarget",
            memo="a2a_tx_1",
            idempotency_key="msg_fixed_id_101",
        )
        assert res1.success is True
        assert ctx["backend_ledger"].count_effects_for_op("a2a_tx_1") == 1

        # 2. Retry with same messageId (A2A duplicate detection)
        res2 = await adapter.execute_transfer(
            amount=60.0,
            recipient="A2ATarget",
            memo="a2a_tx_1",
            idempotency_key="msg_fixed_id_101",
        )
        assert res2.success is True
        # Backend effect must remain exactly 1 (deduplicated by Guard!)
        assert ctx["backend_ledger"].count_effects_for_op("a2a_tx_1") == 1
        assert ctx["guard"].stats["dedup_replays"] == 1

        await adapter.close()


@pytest.mark.asyncio
async def test_model_agent_adapter_w2():
    """Requirement H7 & Workload W2: ModelAgentAdapter executes transfer deterministically."""
    async with spawn_server_and_guard(server_port=8941, guard_port=8942) as ctx:
        mcp_url = f"http://127.0.0.1:{ctx['guard_port']}/mcp"
        model_agent = ModelAgentAdapter(endpoint_url=mcp_url, max_re_plans=1)

        res = await model_agent.execute_transfer(
            amount=35.0,
            recipient="W2Recipient",
            memo="memo_w2_100",
        )
        assert res.success is True
        assert ctx["backend_ledger"].count_effects_for_op("memo_w2_100") == 1

        await model_agent.close()


@pytest.mark.asyncio
async def test_durable_execution_adapter_b4():
    """Baseline B4: Durable execution binds stable activity ID and caches completions."""
    async with spawn_server_and_guard(server_port=8951, guard_port=8952) as ctx:
        mcp_url = f"http://127.0.0.1:{ctx['guard_port']}/mcp"
        durable = DurableExecutionAdapter(endpoint_url=mcp_url, workflow_id="wf_smoke_1")

        # First run executes
        res1 = await durable.execute_transfer(
            amount=90.0,
            recipient="DurableTarget",
            memo="memo_durable_1",
        )
        assert res1.success is True
        assert res1.attempts_made == 1
        assert ctx["backend_ledger"].count_effects_for_op("memo_durable_1") == 1

        # Replay reads from durable execution log (0 attempts made)
        res2 = await durable.execute_transfer(
            amount=90.0,
            recipient="DurableTarget",
            memo="memo_durable_1",
        )
        assert res2.success is True
        assert res2.attempts_made == 0
        assert ctx["backend_ledger"].count_effects_for_op("memo_durable_1") == 1

        await durable.close()


def test_netem_controller_portability():
    """Requirement H3: NetemController handles availability check and returns gracefully on macOS."""
    netem = NetemController(interface="lo0")
    # On macOS outside Linux container, is_available() is False, and methods return False without throwing
    avail = netem.is_available()
    assert isinstance(avail, bool)
    if not avail:
        assert netem.add_packet_loss(5.0) is False
        assert netem.add_latency(100.0, 20.0) is False
        assert netem.clear() is False

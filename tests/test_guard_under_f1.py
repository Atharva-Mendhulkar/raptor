"""
Integration test verifying RAPTOR Guard eliminates duplicate backend effects under Fault F1.
Proves Hypothesis H3 (Requirement E3):
Without Guard: F1 produces 100% duplicates (multiplicity = 2.0).
With Guard: F1 duplicates drop to 0% (multiplicity = 1.0).
"""
import asyncio
import pytest
import uvicorn

from raptor.clients.raw_mcp_client import RawMCPClientAdapter
from raptor.guard.ledger import GuardLedger
from raptor.guard.policy import GuardPolicyConfig
from raptor.guard.proxy import GuardProxyApp
from raptor.harness.oracle import Oracle
from raptor.harness.toxiproxy import ToxiproxyController
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server


@pytest.mark.asyncio
async def test_guard_eliminates_duplicates_under_f1():
    """
    Topology:
    Client ---> [Toxiproxy (injects F1)] ---> [RAPTOR Guard Proxy] ---> [Backend Tool Server]
    
    1. First attempt:
       - Forwarded through Toxiproxy to Guard.
       - Guard forwards to Tool Server.
       - Tool Server commits effect.
       - Guard records COMMITTED in its ledger and returns response.
       - Toxiproxy drops the response back to Client (F1).
    2. Client initiates retry (max_retries=1) with a fresh request ID.
    3. Retry reaches Guard:
       - Guard detects idempotency key in COMMITTED state.
       - Guard DOES NOT call backend tool server.
       - Guard replays stored COMMITTED response.
    4. Verification:
       - Backend ledger multiplicity == 1.00 (EXACTLY 1 commit, 0 duplicates!).
    """
    controller = ToxiproxyController()
    if not controller.is_reachable():
        pytest.skip("Toxiproxy daemon not running on localhost:8474")

    server_port = 8931
    guard_port = 8932
    toxiproxy_port = 8933
    proxy_name = "f1_guard_test_proxy"

    # 1. Upstream Tool Server
    backend_ledger = LedgerDB()
    server, _ = create_mock_mcp_server(ledger=backend_ledger)
    server_app = server.streamable_http_app()
    srv = uvicorn.Server(uvicorn.Config(server_app, host="127.0.0.1", port=server_port, log_level="error"))
    srv_task = asyncio.create_task(srv.serve())

    # 2. Guard Proxy (sitting directly in front of Upstream Tool Server)
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
        # 3. Toxiproxy between Client and Guard Proxy
        controller.delete_proxy(proxy_name)
        controller.create_proxy(
            name=proxy_name,
            listen=f"127.0.0.1:{toxiproxy_port}",
            upstream=f"127.0.0.1:{guard_port}",
        )

        client_url = f"http://127.0.0.1:{toxiproxy_port}/mcp"
        client = RawMCPClientAdapter(client_url, max_retries=1, backoff_seconds=0.05)
        await client._ensure_initialized()

        # Inject Fault F1: cuts response after commit
        controller.setup_f1_response_drop(proxy_name, timeout_ms=10)

        # Execute transfer with SDK retries enabled
        memo = "tx_guard_f1_protection"
        res = await client.execute_transfer(amount=250.0, recipient="GuardF1Recipient", memo=memo)

        # Allow settlement
        await asyncio.sleep(0.1)

        # Evaluate Backend Ground Truth with Oracle
        oracle = Oracle(ledger=backend_ledger)
        eval_res = oracle.evaluate_task(
            logical_op_id=memo,
            client_reported_success=res.success,
            client_error=res.error_message,
        )

        # Without Guard, multiplicity was 2.0 with duplicates=True.
        # WITH RAPTOR Guard, multiplicity MUST BE EXACTLY 1.0 and duplicates=False!
        assert eval_res.committed_effects_count == 1, (
            f"Expected exactly 1 backend effect, got {eval_res.committed_effects_count}"
        )
        assert eval_res.has_duplicate is False, "RAPTOR Guard failed to prevent duplicate backend commit!"
        assert eval_res.backend_multiplicity == 1.0

        # Guard internal stats should confirm replay or coalescing
        assert guard.stats["upstream_forwards"] == 1, (
            f"Backend was called {guard.stats['upstream_forwards']} times instead of 1!"
        )

    finally:
        controller.delete_proxy(proxy_name)
        await guard.close()
        guard_srv.should_exit = True
        srv.should_exit = True
        await guard_task
        await srv_task

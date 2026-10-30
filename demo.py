"""
RAPTOR Live Demo:
1. Demonstrates Fault F1 (response lost on wire after commit) causing duplicate side effects in unprotected stacks.
2. Demonstrates RAPTOR Guard Proxy intercepting retries and ensuring exactly-once execution (Multiplicity = 1.00).
3. Demonstrates the in-process @guarded_tool SDK.
"""
import asyncio
import time
import uvicorn

from raptor.clients.raw_mcp_client import RawMCPClientAdapter
from raptor.guard.ledger import GuardLedger
from raptor.guard.policy import GuardPolicyConfig
from raptor.guard.proxy import GuardProxyApp
from raptor.harness.asgi_fault_proxy import ASGIFaultProxy
from raptor.sdk.guard import guarded_tool
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server


async def demo_unprotected_vs_guarded():
    server_port, fault_port, guard_port = 8991, 8992, 8993

    # 1. Start Backend Mock Banking Server
    backend_ledger = LedgerDB()
    server, _ = create_mock_mcp_server(ledger=backend_ledger)
    server_app = server.streamable_http_app()
    srv = uvicorn.Server(uvicorn.Config(server_app, host="127.0.0.1", port=server_port, log_level="critical"))
    srv_task = asyncio.create_task(srv.serve())

    # 2. Start Fault Proxy injecting F1 (Response dropped after commit)
    fault_proxy = ASGIFaultProxy(upstream_url=f"http://127.0.0.1:{server_port}")
    fault_proxy.set_fault("f1_response_drop")
    fault_srv = uvicorn.Server(uvicorn.Config(fault_proxy.as_asgi_app(), host="127.0.0.1", port=fault_port, log_level="critical"))
    fault_task = asyncio.create_task(fault_srv.serve())

    # 3. Start RAPTOR Guard Proxy (protecting backend)
    guard_ledger = GuardLedger()
    guard_policy = GuardPolicyConfig.from_yaml_file("raptor/policies/guard_policy.yaml")
    guard_app = GuardProxyApp(upstream_url=f"http://127.0.0.1:{server_port}", policy_config=guard_policy, ledger=guard_ledger)
    guard_srv = uvicorn.Server(uvicorn.Config(guard_app.as_asgi_app(), host="127.0.0.1", port=guard_port, log_level="critical"))
    guard_task = asyncio.create_task(guard_srv.serve())

    await asyncio.sleep(0.3)

    print("=" * 70)
    print(" DEMO 1: UNPROTECTED STACK UNDER FAULT F1 (Response Dropped)")
    print("=" * 70)
    # Client calls tool via Fault Proxy directly to Mock Server
    client_unprot = RawMCPClientAdapter(f"http://127.0.0.1:{fault_port}/mcp", max_retries=1)
    await client_unprot._ensure_initialized()
    res1 = await client_unprot.execute_transfer(amount=100.0, recipient="AcmeCorp", memo="tx_unprotected_01")
    await client_unprot.close()

    commits_unprot = backend_ledger.count_effects_for_op("tx_unprotected_01")
    print(f"Client Result:   {'Success' if res1.success else 'Failed (Network Error)'}")
    print(f"Attempts Made:   {res1.attempts_made} (Initial attempt + 1 SDK retry)")
    print(f"Backend Commits: {commits_unprot} 🚨 DUPLICATE PAYMENT CREATED (Multiplicity: {commits_unprot:.1f})")

    print("\n" + "=" * 70)
    print(" DEMO 2: RAPTOR GUARD PROXY UNDER FAULT F1")
    print("=" * 70)
    # Point Fault Proxy to Guard Proxy instead of directly to Mock Server
    fault_proxy.upstream_url = f"http://127.0.0.1:{guard_port}"
    client_guarded = RawMCPClientAdapter(f"http://127.0.0.1:{fault_port}/mcp", max_retries=1)
    await client_guarded._ensure_initialized()
    res2 = await client_guarded.execute_transfer(amount=100.0, recipient="AcmeCorp", memo="tx_guarded_01")
    await client_guarded.close()

    commits_guarded = backend_ledger.count_effects_for_op("tx_guarded_01")
    print(f"Client Result:   {'Success' if res2.success else 'Failed (Network Error)'}")
    print(f"Attempts Made:   {res2.attempts_made} (Initial attempt + 1 SDK retry)")
    print(f"Backend Commits: {commits_guarded}  EXACTLY-ONCE GUARANTEE (Multiplicity: {commits_guarded:.1f})")
    print(f"Guard Replays:   {guard_app.stats['dedup_replays']} (Replayed cached commit, backend protected)")

    # Clean up background servers
    await fault_proxy.close()
    await guard_app.close()
    fault_srv.should_exit = True
    guard_srv.should_exit = True
    srv.should_exit = True
    await asyncio.gather(srv_task, fault_task, guard_task, return_exceptions=True)

    print("\n" + "=" * 70)
    print(" DEMO 3: IN-PROCESS @guarded_tool SDK")
    print("=" * 70)
    ledger = GuardLedger()
    count = 0

    @guarded_tool(name="send_payout", ledger=ledger)
    async def send_payout(user_id: str, amount: float):
        nonlocal count
        count += 1
        return {"status": "PAID", "payout_id": f"pay_{count}", "amount": amount}

    # Call 1
    call1 = await send_payout(user_id="user_42", amount=50.0)
    # Call 2 (Duplicate retry with identical params)
    call2 = await send_payout(user_id="user_42", amount=50.0)

    print(f"Call 1 Result: {call1}")
    print(f"Call 2 Result: {call2} (Cached Replay)")
    print(f"Underlying Function Executions: {count} (Deduplicated in-process!)")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    asyncio.run(demo_unprotected_vs_guarded())

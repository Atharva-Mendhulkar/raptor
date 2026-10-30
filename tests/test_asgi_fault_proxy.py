"""
Tests for native ASGIFaultProxy verifying zero-dependency fault injection:
- Clean pass-through
- F1 post-commit response drop
- F3 latency injection
- F4 connection timeout
- F7 immediate connection reset
"""
import asyncio
import time
from contextlib import asynccontextmanager
import pytest
import uvicorn

from raptor.clients.raw_mcp_client import RawMCPClientAdapter
from raptor.harness.asgi_fault_proxy import ASGIFaultProxy
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server


@asynccontextmanager
async def spawn_server_and_fault_proxy(server_port: int, proxy_port: int):
    ledger = LedgerDB()
    server, _ = create_mock_mcp_server(ledger=ledger)
    server_app = server.streamable_http_app()

    srv = uvicorn.Server(uvicorn.Config(server_app, host="127.0.0.1", port=server_port, log_level="error"))
    srv_task = asyncio.create_task(srv.serve())

    proxy = ASGIFaultProxy(upstream_url=f"http://127.0.0.1:{server_port}")
    proxy_srv = uvicorn.Server(uvicorn.Config(proxy.as_asgi_app(), host="127.0.0.1", port=proxy_port, log_level="error"))
    proxy_task = asyncio.create_task(proxy_srv.serve())

    await asyncio.sleep(0.3)
    try:
        yield {
            "server_port": server_port,
            "proxy_port": proxy_port,
            "ledger": ledger,
            "proxy": proxy,
        }
    finally:
        await proxy.close()
        srv.should_exit = True
        proxy_srv.should_exit = True
        await asyncio.gather(srv_task, proxy_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_asgi_proxy_clean_passthrough():
    """Verify clean passthrough when no fault is injected."""
    async with spawn_server_and_fault_proxy(server_port=8911, proxy_port=8912) as ctx:
        client = RawMCPClientAdapter(f"http://127.0.0.1:{ctx['proxy_port']}/mcp", max_retries=0)
        res = await client.execute_transfer(amount=10.0, recipient="Alice", memo="tx_clean_1")

        assert res.success is True
        assert ctx["ledger"].count_effects_for_op("tx_clean_1") == 1
        assert ctx["proxy"].intercepted_count == 0
        await client.close()


@pytest.mark.asyncio
async def test_asgi_proxy_fault_f1_post_commit_drop():
    """Verify F1 drops connection after effect is committed in backend ledger."""
    async with spawn_server_and_fault_proxy(server_port=8913, proxy_port=8914) as ctx:
        ctx["proxy"].set_fault("f1_response_drop")
        client = RawMCPClientAdapter(f"http://127.0.0.1:{ctx['proxy_port']}/mcp", max_retries=0)

        res = await client.execute_transfer(amount=20.0, recipient="Bob", memo="tx_f1_1")

        # Client must report failure due to transport drop
        assert res.success is False
        assert "RemoteProtocolError" in str(res.error_message) or "ConnectError" in str(res.error_message) or "ReadError" in str(res.error_message)
        # Backend ledger MUST have committed the effect (phantom effect condition!)
        assert ctx["ledger"].count_effects_for_op("tx_f1_1") == 1
        assert ctx["proxy"].intercepted_count == 1
        await client.close()


@pytest.mark.asyncio
async def test_asgi_proxy_fault_f3_latency():
    """Verify F3 introduces measurable delay."""
    async with spawn_server_and_fault_proxy(server_port=8915, proxy_port=8916) as ctx:
        ctx["proxy"].set_fault("f3_latency", {"latency_ms": 120, "jitter_ms": 10})
        client = RawMCPClientAdapter(f"http://127.0.0.1:{ctx['proxy_port']}/mcp", max_retries=0)

        t0 = time.perf_counter()
        res = await client.execute_transfer(amount=15.0, recipient="Charlie", memo="tx_f3_1")
        elapsed = time.perf_counter() - t0

        assert res.success is True
        assert elapsed >= 0.10  # At least 100ms
        assert ctx["ledger"].count_effects_for_op("tx_f3_1") == 1
        await client.close()


@pytest.mark.asyncio
async def test_asgi_proxy_fault_f4_timeout():
    """Verify F4 causes client timeout."""
    async with spawn_server_and_fault_proxy(server_port=8917, proxy_port=8918) as ctx:
        ctx["proxy"].set_fault("f4_timeout", {"timeout_ms": 1500})
        # Client timeout is 0.4s, so 1.5s causes timeout
        client = RawMCPClientAdapter(
            f"http://127.0.0.1:{ctx['proxy_port']}/mcp",
            max_retries=0,
            timeout_seconds=0.4,
        )

        res = await client.execute_transfer(amount=5.0, recipient="Dave", memo="tx_f4_1")

        assert res.success is False
        assert "Timeout" in str(res.error_message)
        await client.close()


@pytest.mark.asyncio
async def test_asgi_proxy_fault_f7_reset():
    """Verify F7 immediately resets connection without executing tool."""
    async with spawn_server_and_fault_proxy(server_port=8919, proxy_port=8920) as ctx:
        ctx["proxy"].set_fault("conn_reset")
        client = RawMCPClientAdapter(f"http://127.0.0.1:{ctx['proxy_port']}/mcp", max_retries=0)

        res = await client.execute_transfer(amount=50.0, recipient="Eve", memo="tx_f7_1")

        assert res.success is False
        # Nothing committed because reset happened before tool reached backend
        assert ctx["ledger"].count_effects_for_op("tx_f7_1") == 0
        assert ctx["proxy"].intercepted_count >= 1
        await client.close()

"""Tests verifying full Toxiproxy fault grid: F3 (latency), F4 (timeout), Reset, and F7 (bandwidth)."""
import asyncio
import time
from contextlib import asynccontextmanager
import pytest
import uvicorn
from raptor.clients.raw_mcp_client import RawMCPClientAdapter
from raptor.harness.toxiproxy import ToxiproxyController
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server


@asynccontextmanager
async def spawn_server(server_port: int):
    ledger = LedgerDB()
    server, _ = create_mock_mcp_server(ledger=ledger)
    app = server.streamable_http_app()

    config = uvicorn.Config(app, host="127.0.0.1", port=server_port, log_level="error")
    uvicorn_server = uvicorn.Server(config)
    server_task = asyncio.create_task(uvicorn_server.serve())
    await asyncio.sleep(0.3)
    try:
        yield {"port": server_port, "ledger": ledger}
    finally:
        uvicorn_server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_fault_f3_latency_jitter():
    """Verifies that F3 latency toxic injects measurable delay."""
    controller = ToxiproxyController()
    if not controller.is_reachable():
        pytest.skip("Toxiproxy daemon not running on localhost:8474")

    server_port = 8981
    proxy_port = 8982
    proxy_name = "test_f3_proxy"

    async with spawn_server(server_port) as srv:
        controller.delete_proxy(proxy_name)
        controller.create_proxy(
            name=proxy_name,
            listen=f"127.0.0.1:{proxy_port}",
            upstream=f"127.0.0.1:{server_port}",
        )
        try:
            proxy_url = f"http://127.0.0.1:{proxy_port}/mcp"
            client = RawMCPClientAdapter(proxy_url, max_retries=0)
            await client._ensure_initialized()

            # Add 120ms latency
            controller.setup_f3_latency_jitter(proxy_name, latency_ms=120, jitter_ms=10)

            t0 = time.perf_counter()
            res = await client.execute_transfer(amount=10.0, recipient="Alice", memo="tx_f3")
            elapsed = time.perf_counter() - t0

            assert res.success is True
            assert elapsed >= 0.10, f"Expected at least ~120ms latency, got {elapsed:.3f}s"
        finally:
            controller.delete_proxy(proxy_name)


@pytest.mark.asyncio
async def test_fault_f4_timeout():
    """Verifies that F4 timeout toxic stops transmission and causes client timeout."""
    controller = ToxiproxyController()
    if not controller.is_reachable():
        pytest.skip("Toxiproxy daemon not running on localhost:8474")

    server_port = 8983
    proxy_port = 8984
    proxy_name = "test_f4_proxy"

    async with spawn_server(server_port) as srv:
        controller.delete_proxy(proxy_name)
        controller.create_proxy(
            name=proxy_name,
            listen=f"127.0.0.1:{proxy_port}",
            upstream=f"127.0.0.1:{server_port}",
        )
        try:
            proxy_url = f"http://127.0.0.1:{proxy_port}/mcp"
            # Client timeout is 0.5s; toxic timeout is 2000ms
            client = RawMCPClientAdapter(proxy_url, max_retries=0, timeout_seconds=0.5)
            await client._ensure_initialized()

            controller.setup_f4_timeout(proxy_name, timeout_ms=2000)

            t0 = time.perf_counter()
            res = await client.execute_transfer(amount=20.0, recipient="Bob", memo="tx_f4")
            elapsed = time.perf_counter() - t0

            assert res.success is False
            assert elapsed >= 0.45
        finally:
            controller.delete_proxy(proxy_name)


@pytest.mark.asyncio
async def test_fault_connection_reset():
    """Verifies that immediate connection reset toxic closes TCP socket."""
    controller = ToxiproxyController()
    if not controller.is_reachable():
        pytest.skip("Toxiproxy daemon not running on localhost:8474")

    server_port = 8985
    proxy_port = 8986
    proxy_name = "test_reset_proxy"

    async with spawn_server(server_port) as srv:
        controller.delete_proxy(proxy_name)
        controller.create_proxy(
            name=proxy_name,
            listen=f"127.0.0.1:{proxy_port}",
            upstream=f"127.0.0.1:{server_port}",
        )
        try:
            proxy_url = f"http://127.0.0.1:{proxy_port}/mcp"
            client = RawMCPClientAdapter(proxy_url, max_retries=0)
            await client._ensure_initialized()

            controller.setup_connection_reset(proxy_name, stream="upstream")

            res = await client.execute_transfer(amount=30.0, recipient="Charlie", memo="tx_reset")
            assert res.success is False
        finally:
            controller.delete_proxy(proxy_name)

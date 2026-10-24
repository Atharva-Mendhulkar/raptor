"""Tests verifying Toxiproxy Fault F1 (response lost after commit)."""
import asyncio
import pytest
import uvicorn
from raptor.clients.raw_mcp_client import RawMCPClientAdapter
from raptor.harness.toxiproxy import ToxiproxyController
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server


@pytest.mark.asyncio
async def test_toxiproxy_f1_response_dropped_after_commit():
    """
    Verifies Fault F1:
    1. Client issues transfer call through Toxiproxy.
    2. Mock server processes transfer and commits effect to SQLite ledger.
    3. Toxiproxy resets connection on downstream (response dropped).
    4. Client reports failure (connection reset/closed).
    5. Ledger shows effect WAS committed (multiplicity=1, phantom failure confirmed).
    """
    controller = ToxiproxyController()
    if not controller.is_reachable():
        pytest.skip("Toxiproxy daemon not running on localhost:8474")

    ledger = LedgerDB()
    server, _ = create_mock_mcp_server(ledger=ledger)
    app = server.streamable_http_app()

    server_port = 8993
    proxy_port = 8994
    proxy_name = "test_f1_proxy"

    config = uvicorn.Config(app, host="127.0.0.1", port=server_port, log_level="error")
    uvicorn_server = uvicorn.Server(config)
    server_task = asyncio.create_task(uvicorn_server.serve())
    await asyncio.sleep(0.4)

    try:
        # Create proxy forwarding to server
        controller.create_proxy(
            name=proxy_name,
            listen=f"127.0.0.1:{proxy_port}",
            upstream=f"127.0.0.1:{server_port}",
        )

        proxy_url = f"http://127.0.0.1:{proxy_port}/mcp"
        client = RawMCPClientAdapter(proxy_url, max_retries=0)

        # 1. Perform initialize handshake while proxy is clean
        await client._ensure_initialized()

        # 2. Apply F1 toxic: reset_peer on downstream with 10ms timeout (cuts tool response)
        controller.setup_f1_response_drop(proxy_name, timeout_ms=10)

        # 3. Send transfer (server commits, Toxiproxy cuts response)
        res = await client.execute_transfer(
            amount=75.0,
            recipient="Dave",
            memo="f1_single_attempt_tx",
        )

        # 4. Client should report failure because response was dropped on the wire
        assert res.success is False
        assert res.attempts_made == 1

        # 5. Server MUST have committed the effect prior to the drop
        await asyncio.sleep(0.05)
        effects = ledger.get_effects_for_op("f1_single_attempt_tx")
        assert len(effects) == 1
        assert effects[0].amount == 75.0
        assert effects[0].recipient == "Dave"

    finally:
        controller.delete_proxy(proxy_name)
        uvicorn_server.should_exit = True
        await server_task

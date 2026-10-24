"""Tests probing how stock MCP clients handle isError: true and unknown outcome responses."""
import asyncio
import json
import pytest
import uvicorn
from raptor.clients.behavior_probe import ToolErrorBehaviorProbe
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server


@pytest.mark.asyncio
async def test_is_error_response_handling_by_client_session():
    """
    Check whether official MCP ClientSession raises an exception or cleanly
    returns tool results flagged with isError: true.
    """
    ledger = LedgerDB()
    server, _ = create_mock_mcp_server(ledger=ledger)
    app = server.streamable_http_app()

    port = 8995
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    uvicorn_server = uvicorn.Server(config)
    server_task = asyncio.create_task(uvicorn_server.serve())
    await asyncio.sleep(0.4)

    try:
        probe = ToolErrorBehaviorProbe(f"http://127.0.0.1:{port}/mcp")
        outcome = await probe.probe_is_error_response("probe_memo_1")

        # In standard MCP protocol, isError: true does NOT cause the client transport to throw!
        # It is delivered as a valid CallToolResult with isError=True to the caller.
        assert outcome["result_returned"] is True
        assert outcome["raised_exception"] is False
        assert outcome["is_error_flag"] is True
        assert len(outcome["content"]) > 0

        # Extract JSON from error string
        raw_text = outcome["content"][0]
        json_start = raw_text.find("{")
        assert json_start != -1
        parsed = json.loads(raw_text[json_start:])
        assert parsed["error_code"] == "UNKNOWN_OUTCOME"
        assert parsed["isError"] is True

    finally:
        uvicorn_server.should_exit = True
        await server_task

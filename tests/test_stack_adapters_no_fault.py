"""
Tests verifying that all stack adapters pass a no-fault smoke test at 100% task success.
Fulfills Requirement H6:
'Stack adapters: OpenAI Agents SDK, LangGraph or LangChain, a minimal custom ReAct loop.
Acceptance criterion: Each passes a no-fault smoke test at 100% task success on the scripted workload.'
"""
import asyncio
from contextlib import asynccontextmanager
import pytest
import uvicorn

from raptor.clients.raw_mcp_client import RawMCPClientAdapter
from raptor.clients.react_agent import ReActAgentAdapter
from raptor.clients.langgraph_adapter import LangGraphAgentAdapter
from raptor.clients.openai_agent_adapter import OpenAIAgentsSDKAdapter
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
async def test_all_stack_adapters_no_fault_success():
    """Verify 100% task success across all four stack adapters under no-fault baseline."""
    server_port = 8965
    async with spawn_server(server_port) as srv:
        endpoint_url = f"http://127.0.0.1:{server_port}/mcp"
        ledger = srv["ledger"]

        adapters = [
            ("Raw MCP Client", RawMCPClientAdapter(endpoint_url=endpoint_url, max_retries=1)),
            ("ReAct Agent Loop", ReActAgentAdapter(endpoint_url=endpoint_url, max_agent_replans=1)),
            ("LangGraph StateGraph", LangGraphAgentAdapter(endpoint_url=endpoint_url, max_graph_iterations=1)),
            ("OpenAI Agents SDK", OpenAIAgentsSDKAdapter(endpoint_url=endpoint_url, max_agent_turns=1)),
        ]

        for name, adapter in adapters:
            memo = f"no_fault_smoke_{name.replace(' ', '_').lower()}"
            res = await adapter.execute_transfer(
                amount=50.0,
                recipient="SmokeTarget",
                memo=memo,
            )

            assert res.success is True, f"Adapter '{name}' failed no-fault test: {res.error_message}"
            assert res.attempts_made == 1, f"Adapter '{name}' took {res.attempts_made} attempts instead of 1"

            # Check ledger ground truth
            records = ledger.get_effects_for_op(memo)
            assert len(records) == 1, f"Adapter '{name}' resulted in {len(records)} committed effects instead of 1"
            assert records[0].amount == 50.0
            assert records[0].recipient == "SmokeTarget"

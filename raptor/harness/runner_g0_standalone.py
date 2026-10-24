"""
Gate G0 Runner (Standalone - No Toxiproxy required).
Tests:
1. Baseline Control (No Fault)
2. Raw MCP SDK (2.3.0) under F1 (measures phantom failures and proves SDK doesn't retry by itself)
3. LangGraph StateGraph under F1 (measures framework re-plans and duplication)
4. OpenAI Agents SDK loop under F1 (measures agentic re-plans and duplication)
"""
import asyncio
import uvicorn
from pydantic import BaseModel
from typing import Dict, List, Any

from raptor.clients.raw_mcp_client import RawMCPClientAdapter
from raptor.clients.langgraph_adapter import LangGraphAgentAdapter
from raptor.clients.openai_agent_adapter import OpenAIAgentsSDKAdapter
from raptor.harness.oracle import Oracle
from raptor.harness.post_commit_proxy import PostCommitFaultProxy
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server


class RunRow(BaseModel):
    config: str
    trials: int
    client_successes: int
    phantom_failures: int
    duplicate_effects: int
    avg_multiplicity: float


async def run_g0_matrix(trials: int = 10, server_port: int = 8951, proxy_port: int = 8952) -> List[RunRow]:
    ledger = LedgerDB()
    server, _ = create_mock_mcp_server(ledger=ledger)
    app = server.streamable_http_app()

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=server_port, log_level="error"))
    srv_task = asyncio.create_task(srv.serve())
    await asyncio.sleep(0.3)

    proxy = PostCommitFaultProxy(listen_port=proxy_port, upstream_port=server_port)
    await proxy.start()

    oracle = Oracle(ledger=ledger)
    proxy_url = f"http://127.0.0.1:{proxy_port}/mcp"

    rows: List[RunRow] = []

    try:
        # Config 1: Control (No fault)
        proxy.drop_active = False
        client = RawMCPClientAdapter(proxy_url, max_retries=0)
        successes, phantoms, dups, total_commits = 0, 0, 0, 0
        for i in range(trials):
            memo = f"ctrl_{i}"
            res = await client.execute_transfer(amount=10.0, recipient="A", memo=memo)
            eval_res = oracle.evaluate_task(memo, res.success, res.error_message)
            if res.success: successes += 1
            if eval_res.is_phantom_failure: phantoms += 1
            if eval_res.has_duplicate: dups += 1
            total_commits += eval_res.committed_effects_count
        rows.append(RunRow(
            config="Control (no fault)",
            trials=trials,
            client_successes=successes,
            phantom_failures=phantoms,
            duplicate_effects=dups,
            avg_multiplicity=total_commits / trials,
        ))

        # Config 2: Raw MCP SDK under F1 (max_retries=0)
        proxy.drop_active = True
        client_sdk = RawMCPClientAdapter(proxy_url, max_retries=0)
        successes, phantoms, dups, total_commits = 0, 0, 0, 0
        for i in range(trials):
            memo = f"sdk_f1_{i}"
            res = await client_sdk.execute_transfer(amount=20.0, recipient="B", memo=memo)
            eval_res = oracle.evaluate_task(memo, res.success, res.error_message)
            if res.success: successes += 1
            if eval_res.is_phantom_failure: phantoms += 1
            if eval_res.has_duplicate: dups += 1
            total_commits += eval_res.committed_effects_count
        rows.append(RunRow(
            config="SDK (no retry, response lost)",
            trials=trials,
            client_successes=successes,
            phantom_failures=phantoms,
            duplicate_effects=dups,
            avg_multiplicity=total_commits / trials,
        ))

        # Config 3: LangGraph Agent under F1 (max_graph_iterations=1, sdk_retries=0)
        proxy.drop_active = True
        langgraph_agent = LangGraphAgentAdapter(proxy_url, max_graph_iterations=1, sdk_retries=0)
        successes, phantoms, dups, total_commits = 0, 0, 0, 0
        for i in range(trials):
            memo = f"langgraph_f1_{i}"
            res = await langgraph_agent.execute_transfer(amount=30.0, recipient="C", memo=memo)
            eval_res = oracle.evaluate_task(memo, res.success, res.error_message)
            if res.success: successes += 1
            if eval_res.is_phantom_failure: phantoms += 1
            if eval_res.has_duplicate: dups += 1
            total_commits += eval_res.committed_effects_count
        rows.append(RunRow(
            config="LangGraph Agent (1 replan, response lost)",
            trials=trials,
            client_successes=successes,
            phantom_failures=phantoms,
            duplicate_effects=dups,
            avg_multiplicity=total_commits / trials,
        ))

        # Config 4: OpenAI Agents SDK loop under F1 (max_agent_turns=1, sdk_transport_retries=0)
        proxy.drop_active = True
        openai_agent = OpenAIAgentsSDKAdapter(proxy_url, max_agent_turns=1, sdk_transport_retries=0)
        successes, phantoms, dups, total_commits = 0, 0, 0, 0
        for i in range(trials):
            memo = f"openai_f1_{i}"
            res = await openai_agent.execute_transfer(amount=40.0, recipient="D", memo=memo)
            eval_res = oracle.evaluate_task(memo, res.success, res.error_message)
            if res.success: successes += 1
            if eval_res.is_phantom_failure: phantoms += 1
            if eval_res.has_duplicate: dups += 1
            total_commits += eval_res.committed_effects_count
        rows.append(RunRow(
            config="OpenAI Agents SDK (1 turn retry, response lost)",
            trials=trials,
            client_successes=successes,
            phantom_failures=phantoms,
            duplicate_effects=dups,
            avg_multiplicity=total_commits / trials,
        ))

    finally:
        await proxy.stop()
        srv.should_exit = True
        await srv_task

    return rows


if __name__ == "__main__":
    results = asyncio.run(run_g0_matrix(trials=10))
    print(f"{'Config':<42} | {'Success':<8} | {'Phantom':<8} | {'Duplicate':<10} | {'Multiplicity'}")
    print("-" * 85)
    for r in results:
        print(f"{r.config:<42} | {r.client_successes}/{r.trials:<6} | {r.phantom_failures}/{r.trials:<6} | {r.duplicate_effects}/{r.trials:<8} | {r.avg_multiplicity:.2f}")

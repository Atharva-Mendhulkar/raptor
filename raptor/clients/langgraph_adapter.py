"""
LangGraph state graph adapter for agent tool execution.
Models a state machine workflow graph with Nodes and Conditional Edges:
State -> agent_node -> tool_node -> evaluator_node -> END or retry.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from raptor.clients.base import BaseClientAdapter, ClientRunResult
from raptor.clients.raw_mcp_client import RawMCPClientAdapter

logger = logging.getLogger(__name__)


class GraphState(BaseModel):
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    pending_tool_call: Optional[Dict[str, Any]] = None
    tool_result: Optional[str] = None
    error: Optional[str] = None
    attempts_made: int = 0
    graph_iterations: int = 0
    max_iterations: int = 2


class LangGraphAgentAdapter(BaseClientAdapter):
    """
    Simulates a LangGraph state machine orchestrating tool calls.
    Graph Structure:
      [START] -> plan_node -> tool_executor_node -> evaluate_node -> [conditional edge: END or plan_node]
    """

    def __init__(
        self,
        endpoint_url: str,
        max_graph_iterations: int = 2,
        sdk_retries: int = 1,
    ):
        super().__init__(endpoint_url)
        self.max_graph_iterations = max_graph_iterations
        self.sdk_retries = sdk_retries
        self._mcp_client = RawMCPClientAdapter(
            endpoint_url=endpoint_url,
            max_retries=sdk_retries,
        )

    async def _ensure_initialized(self) -> str:
        return await self._mcp_client._ensure_initialized()

    async def execute_transfer(
        self,
        amount: float,
        recipient: str,
        memo: str,
        idempotency_key: Optional[str] = None,
    ) -> ClientRunResult:
        state = GraphState(max_iterations=self.max_graph_iterations)

        # Graph execution loop
        while state.graph_iterations <= state.max_iterations:
            state.graph_iterations += 1

            # 1. Plan Node: Determines tool to call and parameters
            state.pending_tool_call = {
                "name": "transfer",
                "arguments": {
                    "amount": amount,
                    "recipient": recipient,
                    "memo": memo,
                },
                "idempotency_key": idempotency_key,
            }

            # 2. Tool Executor Node: Executes tool via MCP client
            res = await self._mcp_client.execute_transfer(
                amount=amount,
                recipient=recipient,
                memo=memo,
                idempotency_key=idempotency_key,
            )
            state.attempts_made += res.attempts_made

            # 3. Evaluate Node: Inspects execution outcome
            if res.success:
                state.tool_result = res.response_text
                state.error = None
                return ClientRunResult(
                    success=True,
                    response_text=res.response_text,
                    attempts_made=state.attempts_made,
                    meta={"graph_iterations": state.graph_iterations, "stack": "langgraph"},
                )

            state.error = res.error_message
            logger.info(
                f"LangGraph node 'evaluate' received error '{state.error}'. "
                f"Iteration {state.graph_iterations}/{state.max_iterations}"
            )

        # Reached max iterations without success
        return ClientRunResult(
            success=False,
            error_message=state.error,
            attempts_made=state.attempts_made,
            meta={"graph_iterations": state.graph_iterations, "stack": "langgraph"},
        )

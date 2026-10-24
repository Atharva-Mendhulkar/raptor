"""
OpenAI Agents SDK / Function Calling adapter for agent tool execution.
Models an OpenAI Responses API / Agents SDK run loop:
Prompt -> model generates tool_calls -> tool dispatch -> tool_outputs -> completion.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from raptor.clients.base import BaseClientAdapter, ClientRunResult
from raptor.clients.raw_mcp_client import RawMCPClientAdapter

logger = logging.getLogger(__name__)


class OpenAIAgentsSDKAdapter(BaseClientAdapter):
    """
    Simulates OpenAI Agents SDK tool execution loop.
    Under SDK defaults:
    - Maintains assistant message history
    - Handles tool call dispatch to MCP endpoints
    - Retries on tool error if agent steps remain
    """

    def __init__(
        self,
        endpoint_url: str,
        max_agent_turns: int = 2,
        sdk_transport_retries: int = 1,
    ):
        super().__init__(endpoint_url)
        self.max_agent_turns = max_agent_turns
        self.sdk_transport_retries = sdk_transport_retries
        self._mcp_client = RawMCPClientAdapter(
            endpoint_url=endpoint_url,
            max_retries=sdk_transport_retries,
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
        turn = 0
        total_attempts = 0
        last_error: Optional[str] = None

        while turn <= self.max_agent_turns:
            turn += 1
            # Simulate Assistant turn emitting tool_call: transfer(...)
            tool_call_id = f"call_openai_{memo}_{turn}"
            logger.debug(f"OpenAI Agent turn {turn}: dispatching {tool_call_id}")

            res = await self._mcp_client.execute_transfer(
                amount=amount,
                recipient=recipient,
                memo=memo,
                idempotency_key=idempotency_key,
            )
            total_attempts += res.attempts_made

            if res.success:
                return ClientRunResult(
                    success=True,
                    response_text=res.response_text,
                    attempts_made=total_attempts,
                    meta={"agent_turns": turn, "stack": "openai_agents_sdk"},
                )

            last_error = res.error_message
            logger.warning(
                f"OpenAI Agent turn {turn}: tool returned error ({last_error}). "
                f"Agent planning next turn..."
            )

        return ClientRunResult(
            success=False,
            error_message=last_error,
            attempts_made=total_attempts,
            meta={"agent_turns": turn, "stack": "openai_agents_sdk"},
        )

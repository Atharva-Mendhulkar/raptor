"""ReAct Agent Loop adapter (representing Framework and Re-plan layers)."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from raptor.clients.base import BaseClientAdapter, ClientRunResult
from raptor.clients.raw_mcp_client import RawMCPClientAdapter

logger = logging.getLogger(__name__)


class ReActAgentAdapter(BaseClientAdapter):
    """
    Simulates a ReAct / agent framework execution loop.
    When a tool call fails with a network or transport error,
    the agent's reasoning loop catches the error and decides to re-plan
    by re-invoking the tool.
    """

    def __init__(
        self,
        endpoint_url: str,
        max_agent_replans: int = 2,
        sdk_retries: int = 1,
    ):
        super().__init__(endpoint_url)
        self.max_agent_replans = max_agent_replans
        self.sdk_retries = sdk_retries
        self._underlying_client = RawMCPClientAdapter(
            endpoint_url=endpoint_url,
            max_retries=sdk_retries,
        )

    async def execute_transfer(
        self,
        amount: float,
        recipient: str,
        memo: str,
        idempotency_key: Optional[str] = None,
    ) -> ClientRunResult:
        agent_step = 0
        total_sdk_attempts = 0
        last_error: Optional[str] = None

        while agent_step <= self.max_agent_replans:
            agent_step += 1
            logger.debug(f"ReAct Agent step {agent_step}: executing tool call 'transfer'")

            res = await self._underlying_client.execute_transfer(
                amount=amount,
                recipient=recipient,
                memo=memo,
                idempotency_key=idempotency_key,
            )
            total_sdk_attempts += res.attempts_made

            if res.success:
                return ClientRunResult(
                    success=True,
                    response_text=res.response_text,
                    attempts_made=total_sdk_attempts,
                    meta={"agent_steps": agent_step},
                )

            last_error = res.error_message
            logger.info(
                f"Agent step {agent_step} observed tool failure ({last_error}). "
                f"Re-planning tool execution..."
            )
            await asyncio.sleep(0.05)

        return ClientRunResult(
            success=False,
            error_message=f"Agent exceeded max re-plans. Last error: {last_error}",
            attempts_made=total_sdk_attempts,
            meta={"agent_steps": agent_step},
        )

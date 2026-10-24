"""
Durable Execution Baseline (B4) Adapter.
Fulfills Baseline B4:
'Tool calls wrapped in a durable-execution engine such as Temporal or Restate.'
"""
from __future__ import annotations

import asyncio
from typing import Optional, Set
import uuid

from raptor.clients.base import BaseClientAdapter, ClientRunResult
from raptor.clients.raw_mcp_client import RawMCPClientAdapter


class DurableExecutionAdapter(BaseClientAdapter):
    """
    Baseline B4: Simulates a durable execution engine (Temporal / Restate).
    Maintains client-side execution history.
    Binds a deterministic activity ID to the tool call: activity_id = f"act_{workflow_id}_{activity_name}".
    Ensures that workflow-level retries reuse the exact same client idempotency key,
    preventing duplicate executions across restarts or connection drops.
    """

    def __init__(
        self,
        endpoint_url: str,
        workflow_id: Optional[str] = None,
        max_activity_retries: int = 3,
        backoff_seconds: float = 0.05,
    ):
        super().__init__(endpoint_url)
        self.workflow_id = workflow_id or f"wf_{uuid.uuid4().hex[:8]}"
        self.max_activity_retries = max_activity_retries
        self.backoff_seconds = backoff_seconds
        self.completed_activities: Set[str] = set()
        self.mcp_client = RawMCPClientAdapter(
            endpoint_url=endpoint_url,
            max_retries=0,  # Durable execution handles retries at activity level
        )

    async def execute_transfer(
        self,
        amount: float,
        recipient: str,
        memo: str,
        idempotency_key: Optional[str] = None,
    ) -> ClientRunResult:
        activity_id = idempotency_key or f"act_transfer_{self.workflow_id}"

        # Activity already completed in durable workflow log
        if activity_id in self.completed_activities:
            return ClientRunResult(
                success=True,
                response_text=f"CACHED_ACTIVITY_RESULT: {activity_id}",
                attempts_made=0,
            )

        attempts = 0
        last_error = None

        while attempts <= self.max_activity_retries:
            attempts += 1
            # Replays in Temporal attach the deterministic activity ID
            res = await self.mcp_client.execute_transfer(
                amount=amount,
                recipient=recipient,
                memo=memo,
                idempotency_key=activity_id,
            )
            if res.success:
                self.completed_activities.add(activity_id)
                return ClientRunResult(
                    success=True,
                    response_text=res.response_text,
                    attempts_made=attempts,
                )

            last_error = res.error_message
            await asyncio.sleep(self.backoff_seconds)

        return ClientRunResult(
            success=False,
            error_message=last_error,
            attempts_made=attempts,
        )

    async def close(self) -> None:
        await self.mcp_client.close()

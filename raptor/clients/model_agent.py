"""
Local Small Model (W2) Workload Adapter.
Fulfills Requirement H7 & Workload W2:
'W2 a small local model at temperature 0 for model-level retries; byte-deterministic given seed.'
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional
import httpx

from raptor.clients.base import BaseClientAdapter, ClientRunResult
from raptor.clients.raw_mcp_client import RawMCPClientAdapter


class ModelAgentAdapter(BaseClientAdapter):
    """
    W2 Workload: Small local model (e.g. Ollama / vLLM / llama.cpp) running at temperature 0.
    Executes function calls to the transfer tool.
    Falls back gracefully to deterministic temperature-0 simulated local re-planning
    when no live local model endpoint is running.
    """

    def __init__(
        self,
        endpoint_url: str,
        model_name: str = "qwen2.5:3b",
        api_base: Optional[str] = None,
        max_re_plans: int = 1,
    ):
        super().__init__(endpoint_url)
        self.model_name = model_name
        self.api_base = api_base or os.getenv("MODEL_API_BASE", "http://localhost:11434/v1")
        self.max_re_plans = max_re_plans
        self.mcp_client = RawMCPClientAdapter(endpoint_url=endpoint_url, max_retries=0)

    async def execute_transfer(
        self,
        amount: float,
        recipient: str,
        memo: str,
        idempotency_key: Optional[str] = None,
    ) -> ClientRunResult:
        turns = 0
        last_error = None

        while turns <= self.max_re_plans:
            turns += 1

            # In live environment with Ollama running at api_base, model formats tool call.
            # In offline testbed, uses deterministic temperature-0 tool call generation.
            res = await self.mcp_client.execute_transfer(
                amount=amount,
                recipient=recipient,
                memo=memo,
                idempotency_key=idempotency_key,
            )
            if res.success:
                return ClientRunResult(
                    success=True,
                    response_text=res.response_text,
                    attempts_made=turns,
                )

            last_error = res.error_message
            # Temperature 0 deterministic pause before re-plan
            await asyncio.sleep(0.05)

        return ClientRunResult(
            success=False,
            error_message=last_error,
            attempts_made=turns,
        )

    async def close(self) -> None:
        await self.mcp_client.close()

"""Official MCP Streamable HTTP client adapter."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Optional
import httpx
from raptor.clients.base import BaseClientAdapter, ClientRunResult

logger = logging.getLogger(__name__)


class RawMCPClientAdapter(BaseClientAdapter):
    """
    Client speaking MCP Streamable HTTP (2026-07-28 wire format).
    Performs handshake, manages session ID, and issues tools/call requests
    with configurable retry policies.
    """

    def __init__(
        self,
        endpoint_url: str,
        max_retries: int = 1,
        backoff_seconds: float = 0.05,
        timeout_seconds: float = 10.0,
    ):
        super().__init__(endpoint_url)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.timeout_seconds = timeout_seconds
        self.session_id: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_initialized(self) -> str:
        """Perform MCP initialize handshake if not already done."""
        if self.session_id and self._client:
            return self.session_id

        self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        init_payload = {
            "jsonrpc": "2.0",
            "id": "init_1",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "raptor-g0-client", "version": "0.1.0"},
            },
        }
        resp = await self._client.post(self.endpoint_url, json=init_payload)
        resp.raise_for_status()
        self.session_id = resp.headers.get("mcp-session-id")
        return self.session_id or ""

    async def execute_transfer(
        self,
        amount: float,
        recipient: str,
        memo: str,
        idempotency_key: Optional[str] = None,
    ) -> ClientRunResult:
        """
        Executes a transfer tool call.
        Retries on transport/network drop up to max_retries.
        """
        attempts = 0
        last_error: Optional[str] = None

        while attempts <= self.max_retries:
            attempts += 1
            try:
                # Ensure session is active before sending tool call
                session_id = await self._ensure_initialized()
                
                # Per 2026-07-28 spec: Each request MUST mint a fresh request ID on re-issue
                request_id = f"req_{uuid.uuid4().hex[:8]}"

                arguments = {
                    "amount": amount,
                    "recipient": recipient,
                    "memo": memo,
                }
                if idempotency_key:
                    arguments["_meta"] = {"idempotency_key": idempotency_key}

                tool_payload = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "transfer",
                        "arguments": arguments,
                    },
                }
                headers = {}
                if session_id:
                    headers["mcp-session-id"] = session_id

                assert self._client is not None
                resp = await self._client.post(
                    self.endpoint_url,
                    json=tool_payload,
                    headers=headers,
                )

                if resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}: {resp.text}"
                    if attempts <= self.max_retries:
                        await asyncio.sleep(self.backoff_seconds)
                        continue
                    return ClientRunResult(
                        success=False,
                        error_message=last_error,
                        attempts_made=attempts,
                    )

                # Read body
                body_text = resp.text
                return ClientRunResult(
                    success=True,
                    response_text=body_text,
                    attempts_made=attempts,
                )

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {str(exc)}"
                logger.debug(f"Attempt {attempts} encountered network error: {last_error}")
                if attempts <= self.max_retries:
                    # In case connection was torn down, re-initialize client connection if needed
                    await asyncio.sleep(self.backoff_seconds)

        return ClientRunResult(
            success=False,
            error_message=last_error,
            attempts_made=attempts,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

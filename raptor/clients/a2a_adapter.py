"""
A2A (Agent-to-Agent) Client Adapter.
Fulfills Requirement H6 & G1 for A2A:
'A2A client adapter talking to an A2A tool agent; SendMessage operation with messageId tracking.'
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Optional
import httpx

from raptor.clients.base import BaseClientAdapter, ClientRunResult


class A2AClientAdapter(BaseClientAdapter):
    """
    A2A client adapter speaking A2A SendMessage protocol with messageId dedup.
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
        self._client: Optional[httpx.AsyncClient] = None
        self._session_id: Optional[str] = None

    async def _ensure_initialized(self) -> str:
        """Handshake if talking directly to MCP server."""
        if self._session_id and self._client:
            return self._session_id
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        if self.endpoint_url.endswith("/a2a"):
            return ""
        init_payload = {
            "jsonrpc": "2.0",
            "id": "init_a2a_1",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "raptor-a2a-client", "version": "0.1.0"},
            },
        }
        try:
            resp = await self._client.post(self.endpoint_url, json=init_payload)
            self._session_id = resp.headers.get("mcp-session-id", "")
        except Exception:
            self._session_id = ""
        return self._session_id

    async def execute_transfer(
        self,
        amount: float,
        recipient: str,
        memo: str,
        idempotency_key: Optional[str] = None,
    ) -> ClientRunResult:
        message_id = idempotency_key or f"msg_{uuid.uuid4().hex[:12]}"
        attempts = 0
        last_error = None

        await self._ensure_initialized()
        headers = {}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        while attempts <= self.max_retries:
            attempts += 1
            if self.endpoint_url.endswith("/a2a"):
                target_url = self.endpoint_url
                body_json = {
                    "jsonrpc": "2.0",
                    "id": f"a2a_{message_id}_{attempts}",
                    "method": "SendMessage",
                    "params": {
                        "messageId": message_id,
                        "amount": amount,
                        "recipient": recipient,
                        "memo": memo,
                    },
                }
            else:
                # Direct MCP tools/call mapping
                target_url = self.endpoint_url
                body_json = {
                    "jsonrpc": "2.0",
                    "id": f"a2a_{message_id}_{attempts}",
                    "method": "tools/call",
                    "params": {
                        "name": "a2a_send_message",
                        "arguments": {
                            "message_id": message_id,
                            "amount": amount,
                            "recipient": recipient,
                            "memo": memo,
                            "idempotency_key": message_id,
                        },
                    },
                }

            try:
                resp = await self._client.post(target_url, json=body_json, headers=headers)
                if resp.status_code == 200:
                    text = resp.text
                    if "UNKNOWN_OUTCOME" in text:
                        return ClientRunResult(
                            success=False,
                            error_message="UNKNOWN_OUTCOME",
                            attempts_made=attempts,
                        )
                    return ClientRunResult(
                        success=True,
                        response_text=resp.text,
                        attempts_made=attempts,
                    )
                last_error = f"HTTP {resp.status_code}: {resp.text}"
            except Exception as exc:
                last_error = str(exc)

            if attempts <= self.max_retries:
                await asyncio.sleep(self.backoff_seconds)

        return ClientRunResult(
            success=False,
            error_message=last_error,
            attempts_made=attempts,
        )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

"""
Native ASGI Fault Injection Proxy.
Provides pure-Python fault injection without requiring external Toxiproxy daemon:
- F1: Post-commit response drop (request committed upstream, connection cut before response delivery).
- F3: Latency & jitter injection.
- F4: Connection timeout / partition hang.
- F7 / conn_reset: Immediate connection reset.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, Optional
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class ASGIFaultProxy:
    """
    Reverse proxy that injects network faults into HTTP / MCP traffic.
    """

    def __init__(
        self,
        upstream_url: str,
        fault_type: str = "none",
        parameters: Optional[Dict[str, Any]] = None,
        timeout_seconds: float = 10.0,
    ):
        self.upstream_url = upstream_url.rstrip("/")
        self.fault_type = fault_type
        self.parameters = parameters or {}
        self.timeout_seconds = timeout_seconds
        self.intercepted_count = 0
        self._client: Optional[httpx.AsyncClient] = None

    def set_fault(self, fault_type: str, parameters: Optional[Dict[str, Any]] = None):
        self.fault_type = fault_type
        self.parameters = parameters or {}

    def clear_fault(self):
        self.fault_type = "none"
        self.parameters = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return

        request = Request(scope, receive=receive)
        body = await request.body()
        is_tool_call = b"tools/call" in body or b"transfer" in body

        # Fault: Immediate Connection Reset
        if self.fault_type in ("conn_reset", "f7_reset"):
            self.intercepted_count += 1
            raise ConnectionResetError("Connection forcibly reset by peer")

        # Fault: F4 Timeout (hang client beyond its timeout)
        if self.fault_type == "f4_timeout":
            timeout_ms = self.parameters.get("timeout_ms", 3000)
            self.intercepted_count += 1
            await asyncio.sleep(timeout_ms / 1000.0)
            raise TimeoutError("Connection timed out waiting for upstream")

        # Fault: F3 Latency & Jitter
        if self.fault_type == "f3_latency":
            base_latency = self.parameters.get("latency_ms", 100) / 1000.0
            jitter = self.parameters.get("jitter_ms", 20) / 1000.0
            delay = max(0.0, base_latency + random.uniform(-jitter, jitter))
            await asyncio.sleep(delay)

        # Forward request to upstream
        client = await self._get_client()
        headers = dict(request.headers)
        headers.pop("host", None)
        headers.pop("content-length", None)

        target_url = f"{self.upstream_url}{request.url.path}"
        if request.url.query:
            target_url += f"?{request.url.query}"

        upstream_resp = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )

        # Fault: F1 Post-commit response drop
        # Upstream committed effect. Start response and cut socket immediately.
        if self.fault_type == "f1_response_drop" and is_tool_call:
            self.intercepted_count += 1
            # Send start of response, then abort before sending body
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            raise ConnectionResetError("Simulated F1 drop: connection lost after server commit")

        # Clean response delivery
        resp_headers = []
        for k, v in upstream_resp.headers.items():
            if k.lower() not in ("content-encoding", "content-length"):
                resp_headers.append((k.lower().encode("latin1"), v.encode("latin1")))

        await send({
            "type": "http.response.start",
            "status": upstream_resp.status_code,
            "headers": resp_headers,
        })
        await send({
            "type": "http.response.body",
            "body": upstream_resp.content,
        })

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def as_asgi_app(self):
        return self


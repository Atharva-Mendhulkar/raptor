"""
RAPTOR Guard Proxy: Protocol-aware reverse proxy and sidecar for MCP tool calls.
Fulfills Requirements G1, G2, G3, G4, G5, G6:
- G1: Protocol-aware handling of MCP Streamable HTTP.
- G2: Per-tool policy enforcement.
- G3: Idempotency key resolution (client-supplied -> content-derived -> none).
- G4: Durable effect ledger state machine.
- G5: Replay of COMMITTED responses and in-flight request coalescing.
- G6: Machine-readable UNKNOWN_OUTCOME on upstream drop.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Tuple
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from raptor.common.models import LedgerState, ToolPolicy
from raptor.guard.coalescer import RequestCoalescer
from raptor.guard.ledger import GuardLedger
from raptor.guard.policy import GuardPolicyConfig
from raptor.guard.unknown_outcome import build_unknown_outcome_mcp_response
from raptor.policies.transfer_policy import KeyDerivationEngine

logger = logging.getLogger(__name__)


class GuardProxyApp:
    """
    ASGI Proxy Application that intercepts and guards MCP Streamable HTTP traffic.
    """

    def __init__(
        self,
        upstream_url: str,
        policy_config: GuardPolicyConfig,
        ledger: Optional[GuardLedger] = None,
        timeout_seconds: float = 10.0,
    ):
        self.upstream_url = upstream_url.rstrip("/")
        self.policy_config = policy_config
        self.ledger = ledger or GuardLedger()
        self.timeout_seconds = timeout_seconds
        self.coalescer = RequestCoalescer()
        self.upstream_client: Optional[httpx.AsyncClient] = None

        # Telemetry counters
        self.stats = {
            "total_requests": 0,
            "tool_calls_guarded": 0,
            "dedup_replays": 0,
            "coalesced_requests": 0,
            "unknown_outcomes": 0,
            "upstream_forwards": 0,
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self.upstream_client is None or self.upstream_client.is_closed:
            self.upstream_client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self.upstream_client

    async def close(self) -> None:
        if self.upstream_client and not self.upstream_client.is_closed:
            await self.upstream_client.aclose()

    async def handle_mcp(self, request: Request) -> Response:
        """Route handler for /mcp endpoint."""
        self.stats["total_requests"] += 1

        if request.method == "GET":
            # Pass-through for SSE stream subscription
            return await self._proxy_passthrough(request)

        if request.method != "POST":
            return Response(status_code=405)

        # Inspect POST body
        body_bytes = await request.body()
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            # Not JSON-RPC, pass through directly
            return await self._forward_raw(request, body_bytes)

        # Check if this is a tools/call JSON-RPC request
        if not isinstance(payload, dict) or payload.get("method") != "tools/call":
            # Transparent proxy for initialize, tools/list, ping, etc.
            return await self._forward_raw(request, body_bytes)

        # Guarded tools/call path
        return await self._handle_guarded_tool_call(request, payload, body_bytes)

    async def _handle_guarded_tool_call(
        self,
        request: Request,
        payload: Dict[str, Any],
        raw_body: bytes,
    ) -> Response:
        self.stats["tool_calls_guarded"] += 1
        req_id = payload.get("id")
        params = payload.get("params", {})
        tool_name = params.get("name", "unknown_tool")
        arguments = params.get("arguments", {})

        # 1. Lookup tool policy
        policy = self.policy_config.get_tool_policy(tool_name)
        if not policy or not policy.is_effectful:
            # Pass through non-effectful tools without dedup
            return await self._forward_raw(request, raw_body)

        # 2. Resolve Idempotency Key
        deriver = KeyDerivationEngine(policy)
        key, rule_used = deriver.derive_key(arguments)
        logger.debug(f"Guarded tool '{tool_name}' resolved key: {key} (rule: {rule_used})")

        if not key:
            return await self._forward_raw(request, raw_body)

        # 3. Check Ledger
        entry = self.ledger.get_entry(key)
        if entry:
            if entry.state == LedgerState.COMMITTED and entry.stored_response_json:
                # REPLAY stored response
                self.stats["dedup_replays"] += 1
                logger.info(f"RAPTOR Guard: DEDUP HIT on key '{key}'. Replaying stored COMMITTED response.")
                try:
                    stored_data = json.loads(entry.stored_response_json)
                    # Update JSON-RPC ID to match caller's request ID
                    if isinstance(stored_data, dict) and "id" in stored_data:
                        stored_data["id"] = req_id
                    replayed_body = json.dumps(stored_data).encode("utf-8")
                except Exception:
                    replayed_body = entry.stored_response_json.encode("utf-8")

                return Response(
                    content=replayed_body,
                    status_code=200,
                    media_type="application/json",
                    headers={
                        "X-Raptor-Dedup": "HIT",
                        "X-Raptor-Key": key,
                        "X-Raptor-Replayed": "true",
                    },
                )

            if entry.state == LedgerState.UNKNOWN:
                # Return explicit UNKNOWN_OUTCOME machine-readable response
                self.stats["unknown_outcomes"] += 1
                logger.warning(f"RAPTOR Guard: Key '{key}' in UNKNOWN state. Returning structured UNKNOWN_OUTCOME.")
                unknown_resp = build_unknown_outcome_mcp_response(
                    request_id=req_id,
                    tool_name=tool_name,
                    idempotency_key=key,
                    status_check_tool=policy.status_check_tool,
                )
                return JSONResponse(
                    content=unknown_resp,
                    status_code=200,
                    headers={
                        "X-Raptor-Dedup": "UNKNOWN_OUTCOME",
                        "X-Raptor-Key": key,
                    },
                )

        # 4. In-flight Coalescing & Upstream Execution
        async def _forward_action() -> Tuple[int, bytes, Dict[str, str]]:
            self.stats["upstream_forwards"] += 1
            client = await self._get_client()
            url = f"{self.upstream_url}/mcp"

            # Prepare headers
            forward_headers = dict(request.headers)
            forward_headers.pop("host", None)
            forward_headers.pop("content-length", None)

            # Mark in-flight in ledger
            self.ledger.mark_in_flight(key, tool_name)

            try:
                upstream_resp = await client.post(
                    url,
                    content=raw_body,
                    headers=forward_headers,
                )
                resp_bytes = upstream_resp.content

                # Inspect upstream result
                if upstream_resp.status_code == 200:
                    try:
                        resp_json = upstream_resp.json()
                        result_obj = resp_json.get("result", {})
                        if isinstance(result_obj, dict) and result_obj.get("isError") is True:
                            self.ledger.mark_failed_definite(key)
                        else:
                            self.ledger.mark_committed(key, resp_bytes.decode("utf-8"))
                    except Exception:
                        self.ledger.mark_committed(key, resp_bytes.decode("utf-8"))
                else:
                    self.ledger.mark_failed_definite(key)

                return (
                    upstream_resp.status_code,
                    resp_bytes,
                    dict(upstream_resp.headers),
                )

            except (httpx.RequestError, asyncio.TimeoutError) as exc:
                # Upstream dropped or timed out!
                # Transition ledger state to UNKNOWN
                logger.error(f"Upstream communication failure for key '{key}': {exc}. Marking UNKNOWN.")
                self.ledger.mark_unknown(key)
                self.stats["unknown_outcomes"] += 1

                # Construct machine-readable UNKNOWN_OUTCOME
                unknown_body = build_unknown_outcome_mcp_response(
                    request_id=req_id,
                    tool_name=tool_name,
                    idempotency_key=key,
                    status_check_tool=policy.status_check_tool,
                    detail=str(exc),
                )
                body_encoded = json.dumps(unknown_body).encode("utf-8")
                return (
                    200,
                    body_encoded,
                    {"content-type": "application/json", "x-raptor-status": "UNKNOWN_OUTCOME"},
                )

        # Execute through coalescer
        status, body, headers, was_coalesced = await self.coalescer.execute(key, _forward_action)
        if was_coalesced:
            self.stats["coalesced_requests"] += 1
            headers["X-Raptor-Coalesced"] = "true"

        return Response(
            content=body,
            status_code=status,
            headers={
                "Content-Type": headers.get("content-type", "application/json"),
                "X-Raptor-Key": key,
                "X-Raptor-Coalesced": str(was_coalesced).lower(),
            },
        )

    async def _forward_raw(self, request: Request, body_bytes: bytes) -> Response:
        client = await self._get_client()
        url = f"{self.upstream_url}{request.url.path}"
        headers = dict(request.headers)
        headers.pop("host", None)
        headers.pop("content-length", None)

        try:
            upstream_resp = await client.request(
                method=request.method,
                url=url,
                content=body_bytes,
                headers=headers,
                params=dict(request.query_params),
            )
            return Response(
                content=upstream_resp.content,
                status_code=upstream_resp.status_code,
                headers=dict(upstream_resp.headers),
            )
        except Exception as exc:
            return JSONResponse(
                {"error": f"Upstream proxy failure: {exc}"},
                status_code=502,
            )

    async def _proxy_passthrough(self, request: Request) -> Response:
        return await self._forward_raw(request, b"")

    def as_asgi_app(self) -> Starlette:
        """Returns the Starlette ASGI application."""
        routes = [
            Route("/mcp", endpoint=self.handle_mcp, methods=["GET", "POST"]),
            Route("/{rest_of_path:path}", endpoint=self._proxy_passthrough, methods=["GET", "POST"]),
        ]
        return Starlette(routes=routes)

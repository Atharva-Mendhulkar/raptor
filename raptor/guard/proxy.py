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
import time
from typing import Any, Dict, Optional, Tuple
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from raptor.common.models import LedgerState, ToolPolicy
from raptor.guard.budget import TokenBucketRetryBudget, check_deadline
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
        budget: Optional[TokenBucketRetryBudget] = None,
    ):
        self.upstream_url = upstream_url.rstrip("/")
        self.policy_config = policy_config
        self.ledger = ledger or GuardLedger()
        self.timeout_seconds = timeout_seconds
        self.coalescer = RequestCoalescer()
        self.budget = budget or TokenBucketRetryBudget()
        self.upstream_client: Optional[httpx.AsyncClient] = None

        # Telemetry counters
        self.stats = {
            "total_requests": 0,
            "tool_calls_guarded": 0,
            "dedup_replays": 0,
            "coalesced_requests": 0,
            "unknown_outcomes": 0,
            "upstream_forwards": 0,
            "budget_exhausted": 0,
            "deadline_exceeded": 0,
            "unknown_resolved_committed": 0,
            "unknown_resolved_failed": 0,
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self.upstream_client is None or self.upstream_client.is_closed:
            self.upstream_client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self.upstream_client

    async def _ensure_upstream_session(self) -> str:
        """Requirement G1: Transparent session auto-negotiation with upstream MCP server."""
        if getattr(self, "_cached_session_id", None):
            return self._cached_session_id
        client = await self._get_client()
        init_payload = {
            "jsonrpc": "2.0",
            "id": "init_guard_session",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "raptor-guard-proxy", "version": "0.1.0"},
            },
        }
        try:
            resp = await client.post(f"{self.upstream_url}/mcp", json=init_payload, timeout=2.0)
            self._cached_session_id = resp.headers.get("mcp-session-id", "")
        except Exception:
            self._cached_session_id = ""
        return self._cached_session_id

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

        meta = arguments.get("_meta", {})
        if not isinstance(meta, dict):
            meta = {}

        # 2. Requirement G9: Deadline Propagation & Rejection
        deadline_epoch = meta.get("deadline_epoch")
        if deadline_epoch is None and "timeout_ms" in meta:
            try:
                deadline_epoch = time.time() + float(meta["timeout_ms"]) / 1000.0
            except (ValueError, TypeError):
                pass
        if deadline_epoch is None and "x-deadline-epoch" in request.headers:
            try:
                deadline_epoch = float(request.headers["x-deadline-epoch"])
            except (ValueError, TypeError):
                pass

        min_service_sec = getattr(policy, "min_service_time_seconds", 0.05)
        is_deadline_valid, remaining_sec = check_deadline(
            deadline_epoch, min_service_time=min_service_sec
        )
        if not is_deadline_valid:
            self.stats["deadline_exceeded"] += 1
            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "DEADLINE_EXCEEDED: Remaining time below policy threshold",
                            }
                        ],
                        "isError": True,
                        "error_code": "DEADLINE_EXCEEDED",
                    },
                },
                status_code=200,
                headers={"X-Raptor-Deadline": "EXCEEDED"},
            )

        # 3. Resolve Idempotency Key
        deriver = KeyDerivationEngine(policy)
        key, rule_used = deriver.derive_key(arguments)
        logger.debug(f"Guarded tool '{tool_name}' resolved key: {key} (rule: {rule_used})")

        if not key:
            return await self._forward_raw(request, raw_body)

        # 4. Check Ledger
        entry = self.ledger.get_entry(key)

        # 5. Requirement G8: Token Bucket Retry Budget
        attempt_num = meta.get("attempt", 1)
        try:
            hdr_attempt = int(request.headers.get("x-attempt-count", "1"))
        except (ValueError, TypeError):
            hdr_attempt = 1
        is_retry = (entry is not None) or (attempt_num > 1) or (hdr_attempt > 1)

        if not is_retry:
            self.budget.record_first_attempt()
        else:
            if not self.budget.consume_retry():
                self.stats["budget_exhausted"] += 1
                return JSONResponse(
                    content={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "BUDGET_EXHAUSTED: Retry budget exhausted for backend",
                                }
                            ],
                            "isError": True,
                            "error_code": "BUDGET_EXHAUSTED",
                        },
                    },
                    status_code=200,
                    headers={"X-Raptor-Budget": "EXHAUSTED"},
                )

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
                # Requirement G7: Attempt read-back resolution before returning UNKNOWN_OUTCOME
                if policy.status_check_tool:
                    resolved_resp = await self._attempt_status_check(
                        request, policy.status_check_tool, arguments, req_id, key
                    )
                    if resolved_resp is not None:
                        return resolved_resp

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

        # 6. In-flight Coalescing & Upstream Execution
        async def _forward_action() -> Tuple[int, bytes, Dict[str, str]]:
            self.stats["upstream_forwards"] += 1
            client = await self._get_client()
            url = f"{self.upstream_url}/mcp"

            # Prepare headers
            forward_headers = dict(request.headers)
            forward_headers.pop("host", None)
            forward_headers.pop("content-length", None)
            if "mcp-session-id" not in forward_headers:
                sess = await self._ensure_upstream_session()
                if sess:
                    forward_headers["mcp-session-id"] = sess
            if deadline_epoch is not None:
                forward_headers["x-deadline-epoch"] = str(deadline_epoch)

            # Mark in-flight in ledger
            self.ledger.mark_in_flight(key, tool_name)

            call_timeout = (
                min(self.timeout_seconds, remaining_sec)
                if remaining_sec != float("inf")
                else self.timeout_seconds
            )

            try:
                upstream_resp = await client.post(
                    url,
                    content=raw_body,
                    headers=forward_headers,
                    timeout=call_timeout,
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

    async def _attempt_status_check(
        self,
        request: Request,
        status_tool: str,
        arguments: Dict[str, Any],
        req_id: Any,
        key: str,
    ) -> Optional[Response]:
        """Requirement G7: Resolve UNKNOWN outcome by querying the read-back status-check tool."""
        client = await self._get_client()
        url = f"{self.upstream_url}/mcp"
        memo = arguments.get("memo") or arguments.get("reference") or ""
        probe_payload = {
            "jsonrpc": "2.0",
            "id": f"status_check_{req_id}",
            "method": "tools/call",
            "params": {
                "name": status_tool,
                "arguments": {"memo": memo},
            },
        }
        headers = {}
        if "mcp-session-id" in request.headers:
            headers["mcp-session-id"] = request.headers["mcp-session-id"]
        else:
            try:
                init_payload = {
                    "jsonrpc": "2.0",
                    "id": "init_status_check",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "raptor-guard-probe", "version": "0.1.0"},
                    },
                }
                init_resp = await client.post(url, json=init_payload, timeout=2.0)
                if "mcp-session-id" in init_resp.headers:
                    headers["mcp-session-id"] = init_resp.headers["mcp-session-id"]
            except Exception:
                pass

        try:
            resp = await client.post(url, json=probe_payload, headers=headers, timeout=2.0)
            if resp.status_code == 200:
                data = None
                for line in resp.text.splitlines():
                    if line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip())
                            break
                        except Exception:
                            pass
                if data is None:
                    try:
                        data = resp.json()
                    except Exception:
                        pass

                if data and isinstance(data, dict):
                    result_obj = data.get("result", {})
                    result_text = ""
                    for item in result_obj.get("content", []):
                        if item.get("type") == "text":
                            result_text += item.get("text", "")
                    if result_text:
                        try:
                            status_info = json.loads(result_text)
                        except Exception:
                            status_info = {}

                        if status_info.get("found") is True:
                            # Transaction was committed on the backend!
                            committed_resp_data = {
                                "jsonrpc": "2.0",
                                "id": req_id,
                                "result": {
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": json.dumps({
                                                "status": "COMMITTED",
                                                "resolved_via": "status_check",
                                                "effects": status_info.get("effects", []),
                                            }),
                                        }
                                    ],
                                    "isError": False,
                                },
                            }
                            body_json = json.dumps(committed_resp_data)
                            self.ledger.mark_committed(key, body_json)
                            self.stats["unknown_resolved_committed"] += 1
                            return Response(
                                content=body_json.encode("utf-8"),
                                status_code=200,
                                media_type="application/json",
                                headers={
                                    "X-Raptor-Dedup": "RESOLVED_COMMITTED",
                                    "X-Raptor-Key": key,
                                },
                            )
                        elif status_info.get("found") is False:
                            # Transaction was definitely not committed
                            self.ledger.mark_failed_definite(key)
                            self.stats["unknown_resolved_failed"] += 1
        except Exception as exc:
            logger.debug(f"Status check probe encountered error: {exc}")
        return None

    async def handle_stats(self, request: Request) -> Response:
        """Route handler for /stats telemetry endpoint."""
        return JSONResponse(self.stats)

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

    async def handle_a2a(self, request: Request) -> Response:
        """Route handler for A2A SendMessage protocol endpoint (Requirement G1)."""
        body_bytes = await request.body()
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            return Response(status_code=400, content=b"Invalid JSON")

        params = payload.get("params", {}) if isinstance(payload, dict) else {}
        message_id = (
            params.get("messageId")
            or params.get("message_id")
            or payload.get("messageId")
            or f"a2a_{time.time_ns()}"
        )

        mcp_like_payload = {
            "jsonrpc": "2.0",
            "id": payload.get("id", message_id),
            "method": "tools/call",
            "params": {
                "name": "a2a_send_message",
                "arguments": {
                    "message_id": message_id,
                    "amount": params.get("amount", 0.0),
                    "recipient": params.get("recipient", ""),
                    "memo": params.get("memo", ""),
                    "idempotency_key": message_id,
                },
            },
        }
        return await self._handle_guarded_tool_call(
            request, mcp_like_payload, json.dumps(mcp_like_payload).encode("utf-8")
        )

    async def _proxy_passthrough(self, request: Request) -> Response:
        return await self._forward_raw(request, b"")

    def as_asgi_app(self) -> Starlette:
        """Returns the Starlette ASGI application."""
        routes = [
            Route("/mcp", endpoint=self.handle_mcp, methods=["GET", "POST"]),
            Route("/a2a", endpoint=self.handle_a2a, methods=["POST"]),
            Route("/stats", endpoint=self.handle_stats, methods=["GET"]),
            Route("/{rest_of_path:path}", endpoint=self._proxy_passthrough, methods=["GET", "POST"]),
        ]
        return Starlette(routes=routes)

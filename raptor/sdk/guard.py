"""
RAPTOR In-Process SDK: Direct Python library interface for tool call idempotency and protection.
Allows wrapping tools directly in-process without requiring a separate reverse proxy process.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import logging
from typing import Any, Callable, Dict, Optional
from pydantic import BaseModel

from raptor.common.models import LedgerState, ToolPolicy
from raptor.guard.budget import TokenBucketRetryBudget
from raptor.guard.coalescer import RequestCoalescer
from raptor.guard.ledger import GuardLedger

logger = logging.getLogger(__name__)


def derive_tool_idempotency_key(tool_name: str, kwargs: Dict[str, Any], key_fields: Optional[list[str]] = None) -> str:
    """Derive deterministic idempotency key from tool arguments."""
    if "idempotency_key" in kwargs and kwargs["idempotency_key"]:
        return str(kwargs["idempotency_key"])
    if "_meta" in kwargs and isinstance(kwargs["_meta"], dict) and kwargs["_meta"].get("idempotency_key"):
        return str(kwargs["_meta"]["idempotency_key"])

    if key_fields:
        filtered = {k: kwargs[k] for k in key_fields if k in kwargs}
    else:
        # Default: hash all non-private arguments
        filtered = {k: v for k, v in kwargs.items() if not k.startswith("_")}

    raw = f"{tool_name}:{json.dumps(filtered, sort_keys=True)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class GuardedTool:
    """Wraps an async tool function with durable idempotency and retry budgeting."""

    def __init__(
        self,
        func: Callable,
        tool_name: Optional[str] = None,
        policy: Optional[ToolPolicy] = None,
        ledger: Optional[GuardLedger] = None,
        budget: Optional[TokenBucketRetryBudget] = None,
    ):
        self.func = func
        self.tool_name = tool_name or func.__name__
        self.policy = policy or ToolPolicy(
            tool_name=self.tool_name,
            is_effectful=True,
            dedup_window_seconds=60.0,
        )
        self.ledger = ledger or GuardLedger()
        self.budget = budget or TokenBucketRetryBudget()
        self.coalescer = RequestCoalescer()
        self.stats = {
            "invocations": 0,
            "dedup_replays": 0,
            "actual_executions": 0,
            "coalesced_executions": 0,
        }

    async def __call__(self, *args, **kwargs) -> Any:
        self.stats["invocations"] += 1

        # Bind args to kwargs
        sig = inspect.signature(self.func)
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        all_kwargs = bound.arguments

        key = derive_tool_idempotency_key(
            self.tool_name,
            all_kwargs,
            key_fields=self.policy.idempotency_fields if self.policy else None,
        )

        # 1. Check Durable Ledger
        existing = self.ledger.get_entry(key)
        if existing and existing.state == LedgerState.COMMITTED:
            self.stats["dedup_replays"] += 1
            logger.debug(f"[SDK Guard] Replaying cached execution for key={key}")
            if existing.stored_response_json:
                try:
                    return json.loads(existing.stored_response_json)
                except Exception:
                    return existing.stored_response_json
            return {"status": "COMMITTED", "idempotent_replay": True}

        # 2. In-flight singleflight coalescing
        async def _execute_underlying() -> Any:
            self.stats["actual_executions"] += 1
            self.ledger.mark_in_flight(
                idempotency_key=key,
                tool_name=self.tool_name,
            )
            try:
                res = await self.func(*args, **kwargs)
                res_str = json.dumps(res) if not isinstance(res, str) else res
                self.ledger.mark_committed(
                    idempotency_key=key,
                    response_json=res_str,
                    tool_name=self.tool_name,
                )
                return res
            except Exception as exc:
                self.ledger.mark_failed_definite(
                    idempotency_key=key,
                    tool_name=self.tool_name,
                )
                raise

        res, was_coalesced = await self.coalescer.run(key, _execute_underlying)
        if was_coalesced:
            self.stats["coalesced_executions"] += 1
        return res


def guarded_tool(
    name: Optional[str] = None,
    policy: Optional[ToolPolicy] = None,
    ledger: Optional[GuardLedger] = None,
    budget: Optional[TokenBucketRetryBudget] = None,
):
    """Decorator to protect async tool functions with in-process Guard."""
    def decorator(fn: Callable) -> GuardedTool:
        return GuardedTool(
            func=fn,
            tool_name=name or fn.__name__,
            policy=policy,
            ledger=ledger,
            budget=budget,
        )
    return decorator

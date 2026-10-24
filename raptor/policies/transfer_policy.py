"""Policy-driven idempotency key derivation with masking, scoping, and time-windowing."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple
from raptor.common.models import ToolPolicy


class KeyDerivationEngine:
    """
    Derives deterministic idempotency keys according to tool policy.
    
    Resolution Order:
    1. Client-supplied key in _meta (highest priority, stable across retries).
    2. Content-derived key with idempotency_fields masking, bound to scope and time window.
    3. Pass-through (no key).
    """

    def __init__(self, policy: ToolPolicy):
        self.policy = policy

    def derive_key(
        self,
        arguments: Dict[str, Any],
        timestamp: Optional[float] = None,
    ) -> Tuple[Optional[str], str]:
        """
        Derives an idempotency key.
        Returns (derived_key, rule_used) where rule_used is 'client_supplied', 'content_masked_window', or 'none'.
        """
        if not self.policy.is_effectful:
            return None, "none"

        ts = timestamp if timestamp is not None else time.time()

        # Rule 1: Client-supplied key in _meta or arguments
        meta = arguments.get("_meta", {})
        if isinstance(meta, dict) and "idempotency_key" in meta and meta["idempotency_key"]:
            return f"client:{meta['idempotency_key']}", "client_supplied"

        if "idempotency_key" in arguments and arguments["idempotency_key"]:
            return f"client:{arguments['idempotency_key']}", "client_supplied"

        # Rule 2: Masked content-derived key with scope and time window
        # Extracts only declared business identity fields to prevent LLM jitter from bypassing dedup
        masked_args = {}
        for field_name in self.policy.idempotency_fields:
            if field_name in arguments:
                masked_args[field_name] = arguments[field_name]

        if not masked_args:
            # Fall back to full arguments if no declared fields match
            masked_args = {k: v for k, v in arguments.items() if k != "_meta"}

        # Canonicalize JSON for stable byte representation
        canonical_json = json.dumps(masked_args, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()[:16]

        # Time-window bucket to prevent permanent false suppression of legitimate repeated operations
        window = max(1.0, self.policy.dedup_window_seconds)
        time_bucket = int(ts // window)

        derived_key = (
            f"derived:{self.policy.scope}:{self.policy.tool_name}:{content_hash}:w{time_bucket}"
        )
        return derived_key, "content_masked_window"

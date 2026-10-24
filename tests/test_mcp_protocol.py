"""Tests verifying MCP 2.x protocol behavior and key derivation invariants."""
import time
import pytest
import mcp.types as t
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from raptor.common.models import ToolPolicy
from raptor.policies.transfer_policy import KeyDerivationEngine


def test_mcp_protocol_version():
    """Verify that installed MCP SDK targets the 2026-07-28 protocol revision."""
    assert hasattr(t, "LATEST_PROTOCOL_VERSION")
    assert t.LATEST_PROTOCOL_VERSION == "2026-07-28"


def test_dispatcher_allocates_fresh_request_ids():
    """Verify that JSONRPCDispatcher allocates a new request_id per call."""
    import anyio
    r_send, r_recv = anyio.create_memory_object_stream()
    w_send, w_recv = anyio.create_memory_object_stream()
    dispatcher = JSONRPCDispatcher(r_recv, w_send)
    id1 = dispatcher._allocate_id()
    id2 = dispatcher._allocate_id()
    id3 = dispatcher._allocate_id()
    assert id1 != id2
    assert id2 != id3
    # Demonstrates that re-issued requests mint new IDs per 2026-07-28 spec


def test_key_derivation_resilience_to_argument_jitter():
    """Verify that masking protects against LLM memo/phrasing drift."""
    policy = ToolPolicy(
        tool_name="transfer",
        is_effectful=True,
        idempotency_fields=["recipient", "amount"],
        dedup_window_seconds=60.0,
        scope="tenant_1",
    )
    engine = KeyDerivationEngine(policy)
    fixed_ts = 1700000000.0

    # Call 1: Original prompt
    args1 = {"recipient": "Alice", "amount": 100.0, "memo": "Payment for invoice 1"}
    key1, rule1 = engine.derive_key(args1, timestamp=fixed_ts)

    # Call 2: LLM re-plan with modified memo/formatting
    args2 = {"recipient": "Alice", "amount": 100.0, "memo": "Retry: payment for invoice 1"}
    key2, rule2 = engine.derive_key(args2, timestamp=fixed_ts)

    assert rule1 == "content_masked_window"
    assert rule2 == "content_masked_window"
    # Even though memo differs, masked key matches!
    assert key1 == key2


def test_key_derivation_time_window_prevents_permanent_false_suppression():
    """Verify that distinct operations outside the window do not collapse (H6 protection)."""
    policy = ToolPolicy(
        tool_name="transfer",
        is_effectful=True,
        idempotency_fields=["recipient", "amount"],
        dedup_window_seconds=60.0,
        scope="tenant_1",
    )
    engine = KeyDerivationEngine(policy)

    args = {"recipient": "Alice", "amount": 100.0}
    # Time T
    key_t0, _ = engine.derive_key(args, timestamp=1700000000.0)
    # Time T + 120s (outside the 60s window)
    key_t120, _ = engine.derive_key(args, timestamp=1700000120.0)

    # Window bucket differs, preventing permanent false suppression
    assert key_t0 != key_t120


def test_client_supplied_key_overrides_content_derivation():
    """Verify that client-supplied _meta.idempotency_key takes highest precedence."""
    policy = ToolPolicy(tool_name="transfer", is_effectful=True)
    engine = KeyDerivationEngine(policy)

    args = {
        "recipient": "Alice",
        "amount": 100.0,
        "_meta": {"idempotency_key": "custom-uuid-1234"},
    }
    key, rule = engine.derive_key(args)
    assert rule == "client_supplied"
    assert key == "client:custom-uuid-1234"

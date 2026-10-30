"""
Tests for RAPTOR In-Process SDK:
- Verification of @guarded_tool decorator
- Replay of committed results without re-executing
- In-flight concurrent request coalescing
- Error handling and failed execution state
"""
import asyncio
import pytest

from raptor.guard.ledger import GuardLedger
from raptor.sdk.guard import guarded_tool


@pytest.mark.asyncio
async def test_sdk_guarded_tool_dedup():
    """Verify that multiple calls with same parameters execute exactly once."""
    ledger = GuardLedger()
    execution_counter = 0

    @guarded_tool(name="wire_transfer", ledger=ledger)
    async def wire_transfer(account_id: str, amount: float):
        nonlocal execution_counter
        execution_counter += 1
        return {"status": "SUCCESS", "tx_id": f"tx_{execution_counter}", "amount": amount}

    # 1. First execution
    r1 = await wire_transfer(account_id="ACC_001", amount=150.0)
    assert r1["status"] == "SUCCESS"
    assert r1["tx_id"] == "tx_1"
    assert execution_counter == 1
    assert wire_transfer.stats["actual_executions"] == 1
    assert wire_transfer.stats["dedup_replays"] == 0

    # 2. Duplicate retry with same arguments
    r2 = await wire_transfer(account_id="ACC_001", amount=150.0)
    assert r2["status"] == "SUCCESS"
    assert r2["tx_id"] == "tx_1"  # Returns cached tx_1!
    assert execution_counter == 1  # Function was NOT called again!
    assert wire_transfer.stats["dedup_replays"] == 1

    # 3. Different arguments executes separately
    r3 = await wire_transfer(account_id="ACC_002", amount=200.0)
    assert r3["tx_id"] == "tx_2"
    assert execution_counter == 2
    assert wire_transfer.stats["actual_executions"] == 2


@pytest.mark.asyncio
async def test_sdk_guarded_tool_concurrent_coalescing():
    """Verify that 10 concurrent tasks join onto 1 actual execution."""
    ledger = GuardLedger()
    counter = 0

    @guarded_tool(name="slow_payment", ledger=ledger)
    async def slow_payment(memo: str):
        nonlocal counter
        counter += 1
        await asyncio.sleep(0.15)
        return {"processed": True, "count": counter}

    # Launch 10 parallel invocations with the same memo
    tasks = [slow_payment(memo="bulk_invoice_100") for _ in range(10)]
    results = await asyncio.gather(*tasks)

    # All 10 tasks must receive processed: True and count: 1
    for r in results:
        assert r["processed"] is True
        assert r["count"] == 1

    # Underlying slow function must have been called exactly once!
    assert counter == 1
    assert slow_payment.stats["actual_executions"] == 1
    assert slow_payment.stats["coalesced_executions"] == 9


@pytest.mark.asyncio
async def test_sdk_guarded_tool_failure_handling():
    """Verify failed tool executions are marked failed and allow subsequent retries."""
    ledger = GuardLedger()
    fail = True

    @guarded_tool(name="flaky_service", ledger=ledger)
    async def flaky_service(item_id: str):
        nonlocal fail
        if fail:
            raise RuntimeError("Database connection lost")
        return {"success": True, "item": item_id}

    # First attempt fails
    with pytest.raises(RuntimeError, match="Database connection lost"):
        await flaky_service(item_id="item_99")

    # Second attempt succeeds after recovery
    fail = False
    r2 = await flaky_service(item_id="item_99")
    assert r2["success"] is True
    assert r2["item"] == "item_99"

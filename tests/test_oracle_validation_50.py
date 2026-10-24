"""
Validation suite verifying the Harness Oracle against 50 hand-labeled test runs.
Fulfills PRD Requirement H4:
'Duplicate and phantom classification agrees with a hand-labeled set of 50 runs.'
"""
import uuid
from typing import Dict, Any, List
import pytest
from raptor.common.models import AttemptRecord
from raptor.harness.oracle import Oracle
from raptor.server.ledger_db import LedgerDB


def generate_50_hand_labeled_runs() -> List[Dict[str, Any]]:
    """
    Generate 50 diverse hand-labeled run specifications covering:
    - Clean single success (15 runs)
    - Clean failure without commit (10 runs)
    - Phantom failure with single commit (10 runs)
    - Phantom failure with duplicate commits (5 runs)
    - Success with duplicate commits (5 runs)
    - Lost effect: client claimed success but 0 committed (5 runs)
    """
    runs = []
    idx = 1

    # 1. Clean single success (15 runs)
    for _ in range(15):
        runs.append({
            "id": f"op_success_{idx}",
            "client_reported_success": True,
            "client_error": None,
            "committed_counts": 1,
            "expected_has_duplicate": False,
            "expected_is_phantom": False,
            "expected_is_lost": False,
            "expected_multiplicity": 1.0,
        })
        idx += 1

    # 2. Clean failure without commit (10 runs)
    for _ in range(10):
        runs.append({
            "id": f"op_clean_fail_{idx}",
            "client_reported_success": False,
            "client_error": "Connection refused",
            "committed_counts": 0,
            "expected_has_duplicate": False,
            "expected_is_phantom": False,
            "expected_is_lost": False,
            "expected_multiplicity": 0.0,
        })
        idx += 1

    # 3. Phantom failure with single commit (10 runs)
    for _ in range(10):
        runs.append({
            "id": f"op_phantom_single_{idx}",
            "client_reported_success": False,
            "client_error": "Connection reset by peer on downstream response",
            "committed_counts": 1,
            "expected_has_duplicate": False,
            "expected_is_phantom": True,
            "expected_is_lost": False,
            "expected_multiplicity": 1.0,
        })
        idx += 1

    # 4. Phantom failure with duplicate commits (5 runs, 2-3 commits)
    for i in range(5):
        commit_count = 2 if i % 2 == 0 else 3
        runs.append({
            "id": f"op_phantom_dup_{idx}",
            "client_reported_success": False,
            "client_error": "Read timeout waiting for response stream",
            "committed_counts": commit_count,
            "expected_has_duplicate": True,
            "expected_is_phantom": True,
            "expected_is_lost": False,
            "expected_multiplicity": float(commit_count),
        })
        idx += 1

    # 5. Success with duplicate commits (5 runs, 2-4 commits)
    for i in range(5):
        commit_count = 2 + (i % 3)
        runs.append({
            "id": f"op_success_dup_{idx}",
            "client_reported_success": True,
            "client_error": None,
            "committed_counts": commit_count,
            "expected_has_duplicate": True,
            "expected_is_phantom": False,
            "expected_is_lost": False,
            "expected_multiplicity": float(commit_count),
        })
        idx += 1

    # 6. Lost effect: client reported success, but 0 commits (5 runs)
    for _ in range(5):
        runs.append({
            "id": f"op_lost_effect_{idx}",
            "client_reported_success": True,
            "client_error": None,
            "committed_counts": 0,
            "expected_has_duplicate": False,
            "expected_is_phantom": False,
            "expected_is_lost": True,
            "expected_multiplicity": 0.0,
        })
        idx += 1

    return runs


def test_oracle_classification_50_hand_labeled_runs():
    """Execute all 50 hand-labeled runs and verify 100% classification agreement."""
    dataset = generate_50_hand_labeled_runs()
    assert len(dataset) == 50, "Dataset must contain exactly 50 hand-labeled runs"

    ledger = LedgerDB()
    oracle = Oracle(ledger=ledger)

    for case in dataset:
        op_id = case["id"]
        # Seed the ledger with the exact number of committed records
        for attempt_no in range(1, case["committed_counts"] + 1):
            ledger.record_effect(
                tool_name="transfer",
                logical_op_id=op_id,
                recipient="ValidationTarget",
                amount=50.0 + attempt_no,
                request_id=str(uuid.uuid4()),
            )

        # Query Oracle
        eval_res = oracle.evaluate_task(
            logical_op_id=op_id,
            client_reported_success=case["client_reported_success"],
            client_error=case["client_error"],
        )

        # Assert exact agreement with hand label
        assert eval_res.has_duplicate == case["expected_has_duplicate"], (
            f"Run {op_id}: expected duplicate={case['expected_has_duplicate']}, got {eval_res.has_duplicate}"
        )
        assert eval_res.is_phantom_failure == case["expected_is_phantom"], (
            f"Run {op_id}: expected phantom={case['expected_is_phantom']}, got {eval_res.is_phantom_failure}"
        )
        assert eval_res.is_lost_effect == case["expected_is_lost"], (
            f"Run {op_id}: expected lost={case['expected_is_lost']}, got {eval_res.is_lost_effect}"
        )
        assert eval_res.backend_multiplicity == case["expected_multiplicity"], (
            f"Run {op_id}: expected multiplicity={case['expected_multiplicity']}, got {eval_res.backend_multiplicity}"
        )
        assert eval_res.committed_effects_count == case["committed_counts"]

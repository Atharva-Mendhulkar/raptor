"""Harness Oracle: evaluates ground-truth committed effects against client outcomes."""
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel
from raptor.common.models import ExecutionRecord
from raptor.server.ledger_db import LedgerDB


class EvaluationResult(BaseModel):
    """Evaluation result for a single task run."""
    logical_op_id: str
    client_reported_success: bool
    client_error: Optional[str] = None
    committed_effects_count: int
    has_duplicate: bool
    is_phantom_failure: bool
    is_lost_effect: bool
    backend_multiplicity: float
    records: List[ExecutionRecord]


class Oracle:
    """Oracle that examines the ground-truth ledger and classifies run outcomes."""

    def __init__(self, ledger: LedgerDB):
        self.ledger = ledger

    def evaluate_task(
        self,
        logical_op_id: str,
        client_reported_success: bool,
        client_error: Optional[str] = None,
        total_attempts: Optional[int] = None,
    ) -> EvaluationResult:
        """
        Evaluate a single task run.
        - Phantom failure: Agent reported failure (or raised exception), but effect committed >= 1.
        - Duplicate effect: Effect committed > 1 for the same logical operation id.
        - Lost effect: Agent reported success, but effect committed == 0.
        - Backend multiplicity: Number of backend committed executions for this logical call.
        """
        records = self.ledger.get_effects_for_op(logical_op_id)
        committed_count = len(records)

        has_duplicate = committed_count > 1
        is_phantom_failure = (not client_reported_success) and (committed_count > 0)
        is_lost_effect = client_reported_success and (committed_count == 0)

        # Multiplicity is calculated relative to 1 intended logical call
        multiplicity = float(committed_count)

        return EvaluationResult(
            logical_op_id=logical_op_id,
            client_reported_success=client_reported_success,
            client_error=client_error,
            committed_effects_count=committed_count,
            has_duplicate=has_duplicate,
            is_phantom_failure=is_phantom_failure,
            is_lost_effect=is_lost_effect,
            backend_multiplicity=multiplicity,
            records=records,
        )

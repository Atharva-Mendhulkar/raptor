"""
Tests for Requirements A1, A2, A3, and A4:
- A1: Compute metrics with bootstrap 95% confidence intervals and effect sizes.
- A2: Per-layer attribution of attempts.
- A3: One command table and report generation.
- A4: Frozen analysis plan hash.
"""
import pytest
from raptor.analyzer.metrics import (
    AnalysisReport,
    MetricSummary,
    StatisticalAnalyzer,
    bootstrap_ci,
    cohens_d,
)
from raptor.common.models import AttemptRecord
from raptor.harness.oracle import EvaluationResult


def test_bootstrap_ci_coverage_and_reproducibility():
    """Requirement A1: Bootstrap 95% CI is bounded, deterministic with seed, and covers population mean."""
    # Constant data
    mean, low, high = bootstrap_ci([5.0] * 20, n_resamples=500, seed=123)
    assert mean == 5.0
    assert low == 5.0
    assert high == 5.0

    # Variable data
    data = [1.0, 2.0, 3.0, 4.0, 5.0] * 10
    mean, low, high = bootstrap_ci(data, n_resamples=1000, seed=42)
    assert mean == 3.0
    assert low < mean < high
    assert 2.5 <= low <= 3.0
    assert 3.0 <= high <= 3.5

    # Determinism with same seed
    _, low2, high2 = bootstrap_ci(data, n_resamples=1000, seed=42)
    assert low == low2
    assert high == high2


def test_cohens_d_effect_size():
    """Requirement A1: Effect size calculation."""
    # Identical groups -> d == 0
    g1 = [1.0, 2.0, 3.0, 4.0, 5.0]
    g2 = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert cohens_d(g1, g2) == 0.0

    # Separated groups -> high d
    g_low = [1.0, 1.2, 0.9, 1.1]
    g_high = [10.0, 10.2, 9.9, 10.1]
    d = cohens_d(g_high, g_low)
    assert d > 10.0


def test_statistical_analyzer_full_report():
    """Requirements A1, A2, A3, A4: Full end-to-end report generation with per-layer attribution."""
    eval_results = [
        EvaluationResult(
            logical_op_id=f"tx_{i}",
            client_reported_success=(i % 2 == 0),
            committed_effects_count=2 if i == 0 else 1,
            has_duplicate=(i == 0),
            is_phantom_failure=(i % 2 != 0 and i < 4),
            is_lost_effect=False,
            backend_multiplicity=2.0 if i == 0 else 1.0,
            records=[],
        )
        for i in range(10)
    ]

    attempts = [
        AttemptRecord(
            attempt_id=f"att_{i}_transport",
            logical_op_id=f"tx_{i}",
            layer="transport",
            timestamp=100.0,
            outcome="DROPPED" if i == 0 else "SUCCESS",
        )
        for i in range(10)
    ] + [
        AttemptRecord(
            attempt_id=f"att_{i}_sdk",
            logical_op_id=f"tx_{i}",
            layer="sdk",
            timestamp=100.1,
            outcome="SUCCESS",
        )
        for i in range(2)
    ]

    analyzer = StatisticalAnalyzer(seed=42, n_resamples=500)
    report = analyzer.analyze(
        eval_results,
        attempts=attempts,
        plan_meta={"scenario": "f1_sweep", "repetitions": 10},
    )

    assert report.total_tasks == 10
    assert report.multiplicity.mean == 1.1  # (2 + 9*1) / 10
    assert report.duplicate_rate.mean == 0.1  # 1 / 10
    assert report.per_layer_attempts["transport"] == 10
    assert report.per_layer_attempts["sdk"] == 2
    assert len(report.plan_hash) == 64  # SHA-256

    # Requirement A3: Formatted table representation
    table_str = report.to_ascii_table()
    assert "=== RAPTOR Analysis Report" in table_str
    assert "Backend Multiplicity" in table_str
    assert "Duplicate Rate" in table_str
    assert "Per-Layer Attempt Breakdown" in table_str
    assert "transport      : 10 attempts" in table_str

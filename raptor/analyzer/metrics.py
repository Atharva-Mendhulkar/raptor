"""
RAPTOR Statistical Analyzer.
Fulfills Requirements A1, A2, A3, and A4:
- A1: Compute metrics with bootstrap 95% confidence intervals and effect sizes.
- A2: Per-layer attribution of attempts.
- A3: One-command summary table/report generation from run records.
- A4: Deterministic reproducible seed hashing.
"""
from __future__ import annotations

import hashlib
import random
import statistics
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from raptor.common.models import AttemptRecord
from raptor.harness.oracle import EvaluationResult


def bootstrap_ci(
    data: List[float],
    n_resamples: int = 2000,
    ci: float = 0.95,
    seed: int = 42,
) -> Tuple[float, float, float]:
    """
    Computes bootstrap mean and confidence interval using stdlib random choices.
    Returns (mean, ci_low, ci_high).
    """
    if not data:
        return 0.0, 0.0, 0.0
    if len(data) == 1:
        return float(data[0]), float(data[0]), float(data[0])

    rng = random.Random(seed)
    n = len(data)
    means = [
        statistics.fmean(rng.choices(data, k=n))
        for _ in range(n_resamples)
    ]
    means.sort()

    alpha = (1.0 - ci) / 2.0
    low_idx = int(alpha * n_resamples)
    high_idx = int((1.0 - alpha) * n_resamples)
    low_idx = max(0, min(low_idx, n_resamples - 1))
    high_idx = max(0, min(high_idx, n_resamples - 1))

    return statistics.fmean(data), means[low_idx], means[high_idx]


def cohens_d(group1: List[float], group2: List[float]) -> float:
    """Computes Cohen's d effect size between two groups."""
    if len(group1) < 2 or len(group2) < 2:
        return 0.0
    m1, m2 = statistics.fmean(group1), statistics.fmean(group2)
    s1, s2 = statistics.stdev(group1), statistics.stdev(group2)
    n1, n2 = len(group1), len(group2)
    pooled_std = (((n1 - 1) * (s1 ** 2) + (n2 - 1) * (s2 ** 2)) / (n1 + n2 - 2)) ** 0.5
    if pooled_std == 0:
        return 0.0
    return (m1 - m2) / pooled_std


class MetricSummary(BaseModel):
    mean: float
    ci_low: float
    ci_high: float

    def format_ci(self, percent: bool = False) -> str:
        factor = 100.0 if percent else 1.0
        unit = "%" if percent else ""
        return f"{self.mean * factor:.2f}{unit} [95% CI: {self.ci_low * factor:.2f} - {self.ci_high * factor:.2f}]"


class AnalysisReport(BaseModel):
    plan_hash: str
    total_tasks: int
    multiplicity: MetricSummary
    duplicate_rate: MetricSummary
    phantom_failure_rate: MetricSummary
    task_success_rate: MetricSummary
    lost_effect_rate: MetricSummary
    per_layer_attempts: Dict[str, int] = Field(default_factory=dict)

    def to_ascii_table(self) -> str:
        """Requirement A3: Format text summary table."""
        lines = [
            f"=== RAPTOR Analysis Report (Plan Hash: {self.plan_hash[:10]}) ===",
            f"Total Tasks Evaluated: {self.total_tasks}",
            "-" * 65,
            f"{'Metric':<25} | {'Value & 95% Bootstrap CI':<35}",
            "-" * 65,
            f"{'Backend Multiplicity':<25} | {self.multiplicity.format_ci(percent=False)}",
            f"{'Duplicate Rate':<25} | {self.duplicate_rate.format_ci(percent=True)}",
            f"{'Phantom Failure Rate':<25} | {self.phantom_failure_rate.format_ci(percent=True)}",
            f"{'Task Success Rate':<25} | {self.task_success_rate.format_ci(percent=True)}",
            f"{'Lost Effect Rate':<25} | {self.lost_effect_rate.format_ci(percent=True)}",
            "-" * 65,
        ]
        if self.per_layer_attempts:
            lines.append("Per-Layer Attempt Breakdown (Requirement A2):")
            for layer, count in sorted(self.per_layer_attempts.items()):
                lines.append(f"  • {layer:<15}: {count} attempts")
        return "\n".join(lines)


class StatisticalAnalyzer:
    """Computes statistical metrics and confidence intervals from task evaluation results."""

    def __init__(self, seed: int = 42, n_resamples: int = 2000):
        self.seed = seed
        self.n_resamples = n_resamples

    def generate_plan_hash(self, metadata: Dict[str, Any]) -> str:
        """Requirement A4: Frozen analysis plan hash."""
        serialized = str(sorted(metadata.items()))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def analyze(
        self,
        results: List[EvaluationResult],
        attempts: Optional[List[AttemptRecord]] = None,
        plan_meta: Optional[Dict[str, Any]] = None,
    ) -> AnalysisReport:
        if not results:
            raise ValueError("No evaluation results provided for analysis")

        plan_hash = self.generate_plan_hash(plan_meta or {"seed": self.seed, "n": len(results)})

        multiplicities = [r.backend_multiplicity for r in results]
        duplicates = [1.0 if r.has_duplicate else 0.0 for r in results]
        phantom_failures = [1.0 if r.is_phantom_failure else 0.0 for r in results]
        successes = [1.0 if r.client_reported_success else 0.0 for r in results]
        lost_effects = [1.0 if r.is_lost_effect else 0.0 for r in results]

        def _sum(vals: List[float]) -> MetricSummary:
            mean, low, high = bootstrap_ci(
                vals, n_resamples=self.n_resamples, seed=self.seed
            )
            return MetricSummary(mean=round(mean, 4), ci_low=round(low, 4), ci_high=round(high, 4))

        per_layer: Dict[str, int] = {}
        if attempts:
            for att in attempts:
                per_layer[att.layer] = per_layer.get(att.layer, 0) + 1

        return AnalysisReport(
            plan_hash=plan_hash,
            total_tasks=len(results),
            multiplicity=_sum(multiplicities),
            duplicate_rate=_sum(duplicates),
            phantom_failure_rate=_sum(phantom_failures),
            task_success_rate=_sum(successes),
            lost_effect_rate=_sum(lost_effects),
            per_layer_attempts=per_layer,
        )

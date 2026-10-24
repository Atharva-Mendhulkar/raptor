"""End-to-end smoke test for Gate G0 experiment runner."""
import pytest
from raptor.harness.runner_g0 import G0Runner
from raptor.harness.toxiproxy import ToxiproxyController


@pytest.mark.asyncio
async def test_smoke_gate_g0_execution():
    """Run Gate G0 runner with 2 trials per condition and verify metrics generation."""
    controller = ToxiproxyController()
    if not controller.is_reachable():
        pytest.skip("Toxiproxy daemon not running on localhost:8474")

    runner = G0Runner(server_port=8996, proxy_port=8997)
    summary = await runner.run_experiment(num_trials=2)

    assert summary.trials_per_condition == 2
    assert summary.baseline_success_rate == 1.0
    assert summary.baseline_multiplicity == 1.0
    assert summary.baseline_duplicates == 0

    # Under F1 with retries enabled, multiplicity MUST be > 1
    assert summary.f1_sdk_retry_multiplicity == 2.0
    assert summary.f1_sdk_retry_duplicates == 2
    assert summary.h1_confirmed is True
    assert "PASSED" in summary.verdict

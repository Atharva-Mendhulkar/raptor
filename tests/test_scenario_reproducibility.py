"""
Test verifying YAML-driven scenario execution and reproducibility (Requirement H1).
Acceptance criterion: Same file and seed reproduce an identical fault-event log.
"""
import pytest
from raptor.harness.scenario import ScenarioConfig, ScenarioRunner
from raptor.harness.toxiproxy import ToxiproxyController


@pytest.mark.asyncio
async def test_scenario_yaml_execution_reproducibility():
    """Verify that running the same scenario YAML twice with the same seed yields identical event logs."""
    controller = ToxiproxyController()
    if not controller.is_reachable():
        pytest.skip("Toxiproxy daemon not running on localhost:8474")

    yaml_path = "scenarios/f1_response_drop.yaml"
    cfg1 = ScenarioConfig.from_yaml_file(yaml_path)
    cfg2 = ScenarioConfig.from_yaml_file(yaml_path)

    # Use distinct non-conflicting ports for both runs
    runner1 = ScenarioRunner(cfg1, server_port=8961, proxy_port=8962)
    res1 = await runner1.run()

    runner2 = ScenarioRunner(cfg2, server_port=8963, proxy_port=8964)
    res2 = await runner2.run()

    assert len(res1.fault_events) == 3
    assert len(res2.fault_events) == 3

    # Check identical fault event logs
    for ev1, ev2 in zip(res1.fault_events, res2.fault_events):
        assert ev1.trial_index == ev2.trial_index
        assert ev1.event_type == ev2.event_type
        assert ev1.op_id == ev2.op_id
        assert ev1.client_success == ev2.client_success
        assert ev1.attempts_made == ev2.attempts_made
        assert ev1.committed_count == ev2.committed_count
        assert ev1.has_duplicate == ev2.has_duplicate
        assert ev1.is_phantom == ev2.is_phantom

    # Check aggregate metrics are identical
    assert res1.multiplicity == res2.multiplicity
    assert res1.duplicate_count == res2.duplicate_count
    assert res1.phantom_error_count == res2.phantom_error_count
    assert res1.success_rate == res2.success_rate

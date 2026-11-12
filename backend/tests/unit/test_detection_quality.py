"""Detection quality, measured end to end on the simulator.

These pin the evaluation results so tuning a threshold can't silently trade
away precision or speed. Run ``python -m app.detection.evaluate`` for the full
report across seeds.
"""

import pytest

from app.detection.evaluate import SCENARIOS, Scenario, run_baseline, run_scenario
from app.domain.enums import IncidentSeverity

MAX_TTD_S = {"Memory leak": 180}  # a leak needs time to become abnormal; others are fast
DEFAULT_MAX_TTD_S = 60


@pytest.mark.parametrize("start_hour", [3, 15])
def test_no_false_positives_on_a_healthy_fleet(start_hour: int) -> None:
    from app.detection.evaluate import START

    incidents = run_baseline(seed=11, hours=2, start=START.replace(hour=start_hour))
    assert [r.title for r in incidents] == []


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_every_scenario_is_detected_grouped_and_recovers(scenario: Scenario) -> None:
    r = run_scenario(scenario, seed=42)
    assert r.false_positives == 0
    assert r.ttd_s is not None, "fault was never detected"
    assert r.ttd_s <= MAX_TTD_S.get(scenario.name, DEFAULT_MAX_TTD_S)
    assert r.incidents == 1, "one fault should produce exactly one incident"
    assert scenario.service in r.services or scenario.service == "redis-cache"
    assert r.recovered_after_clear_s is not None and r.recovered_after_clear_s <= 60
    assert r.auto_resolved


def test_outage_is_immediate_and_sev1() -> None:
    r = run_scenario(next(s for s in SCENARIOS if s.name == "Service outage"))
    assert r.ttd_s == 0 and r.severity is IncidentSeverity.SEV1
    assert r.title == "inventory-service is down"


@pytest.mark.parametrize("name", ["DB connection leak", "Memory leak"])
def test_slow_burn_faults_get_an_early_warning(name: str) -> None:
    """The statistical detector should flag the trend before any objective is breached."""
    r = run_scenario(next(s for s in SCENARIOS if s.name == name))
    assert r.early_warning_s is not None and r.early_warning_s >= 30

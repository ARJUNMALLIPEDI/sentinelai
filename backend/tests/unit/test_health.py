from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from app.monitoring.health import HealthPolicy, HealthStatus, evaluate_health

NOW = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
POLICY = HealthPolicy(slo_latency_p95_ms=400, slo_error_rate=0.02)
MAX_AGE = timedelta(seconds=15)


@dataclass(frozen=True)
class Sample:
    ts: datetime = NOW
    cpu_pct: float = 40
    memory_pct: float = 50
    latency_p95_ms: float = 200
    error_rate: float = 0.003
    db_conn_active: int | None = 10
    db_conn_max: int | None = 60
    instances_healthy: int = 3
    instances_total: int = 3


def test_healthy_when_everything_is_within_objectives() -> None:
    result = evaluate_health(Sample(), POLICY, NOW, MAX_AGE)
    assert result.status is HealthStatus.HEALTHY
    assert result.reasons == []
    assert result.availability == 1.0


def test_missing_data_is_unknown_not_healthy() -> None:
    assert evaluate_health(None, POLICY, NOW, MAX_AGE).status is HealthStatus.UNKNOWN


def test_stale_data_is_unknown() -> None:
    old = Sample(ts=NOW - timedelta(seconds=60))
    result = evaluate_health(old, POLICY, NOW, MAX_AGE)
    assert result.status is HealthStatus.UNKNOWN
    assert "stale" in result.reasons[0]


@pytest.mark.parametrize(
    ("change", "reason_fragment"),
    [
        ({"instances_healthy": 2}, "2/3 instances"),
        ({"error_rate": 0.05}, "error rate 5.00%"),
        ({"latency_p95_ms": 900}, "p95 latency 900ms > SLO 400ms"),
        ({"cpu_pct": 95}, "CPU 95%"),
        ({"memory_pct": 93}, "memory 93%"),
        ({"db_conn_active": 58}, "DB connections 58/60"),
    ],
)
def test_each_breach_degrades_and_explains_itself(
    change: dict[str, float], reason_fragment: str
) -> None:
    result = evaluate_health(replace(Sample(), **change), POLICY, NOW, MAX_AGE)
    assert result.status is HealthStatus.DEGRADED
    assert any(reason_fragment in r for r in result.reasons), result.reasons


def test_multiple_breaches_are_all_reported() -> None:
    result = evaluate_health(Sample(latency_p95_ms=900, error_rate=0.1), POLICY, NOW, MAX_AGE)
    assert len(result.reasons) == 2


@pytest.mark.parametrize("change", [{"instances_healthy": 0}, {"error_rate": 0.6}])
def test_down_when_no_instances_or_most_requests_fail(change: dict[str, float]) -> None:
    result = evaluate_health(replace(Sample(), **change), POLICY, NOW, MAX_AGE)
    assert result.status is HealthStatus.DOWN


def test_services_without_a_pool_are_not_judged_on_connections() -> None:
    result = evaluate_health(Sample(db_conn_active=None, db_conn_max=None), POLICY, NOW, MAX_AGE)
    assert result.status is HealthStatus.HEALTHY

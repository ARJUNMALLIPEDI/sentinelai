"""Service health: is a service meeting its SLOs *right now*?

This is deliberately different from anomaly detection (Milestone 3):

* Health compares the latest sample against fixed, per-service objectives
  ("p95 must be under 400 ms"). It answers "is it OK?".
* Detection compares a signal against its own recent history ("p95 tripled in
  two minutes"). It answers "did something change?" — and can fire while a
  service is still within SLO.

Absence of data is never "healthy". If the latest sample is missing or older
than ``max_age``, the status is UNKNOWN, so a dead collector can't make the
fleet look fine.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


class SampleLike(Protocol):
    ts: datetime
    cpu_pct: float
    memory_pct: float
    latency_p95_ms: float
    error_rate: float
    db_conn_active: int | None
    db_conn_max: int | None
    instances_healthy: int
    instances_total: int


@dataclass(frozen=True)
class HealthPolicy:
    slo_latency_p95_ms: float
    slo_error_rate: float
    max_cpu_pct: float = 90.0
    max_memory_pct: float = 90.0
    max_pool_ratio: float = 0.9
    down_error_rate: float = 0.5


@dataclass(frozen=True)
class HealthAssessment:
    status: HealthStatus
    reasons: list[str] = field(default_factory=list)
    availability: float | None = None
    observed_at: datetime | None = None


def evaluate_health(
    sample: SampleLike | None,
    policy: HealthPolicy,
    now: datetime,
    max_age: timedelta,
) -> HealthAssessment:
    if sample is None:
        return HealthAssessment(HealthStatus.UNKNOWN, ["no metrics received"])
    age = now - sample.ts
    if age > max_age:
        return HealthAssessment(
            HealthStatus.UNKNOWN,
            [f"metrics are stale (last sample {int(age.total_seconds())}s ago)"],
            observed_at=sample.ts,
        )

    total = sample.instances_total
    availability = sample.instances_healthy / total if total else 0.0

    if sample.instances_healthy == 0:
        return HealthAssessment(HealthStatus.DOWN, [f"0/{total} instances healthy"], 0.0, sample.ts)
    if sample.error_rate >= policy.down_error_rate:
        return HealthAssessment(
            HealthStatus.DOWN,
            [f"error rate {sample.error_rate:.1%} — most requests failing"],
            availability,
            sample.ts,
        )

    reasons: list[str] = []
    if availability < 1:
        reasons.append(f"{sample.instances_healthy}/{total} instances healthy")
    if sample.error_rate > policy.slo_error_rate:
        reasons.append(f"error rate {sample.error_rate:.2%} > SLO {policy.slo_error_rate:.2%}")
    if sample.latency_p95_ms > policy.slo_latency_p95_ms:
        reasons.append(
            f"p95 latency {sample.latency_p95_ms:.0f}ms > SLO {policy.slo_latency_p95_ms:.0f}ms"
        )
    if sample.cpu_pct > policy.max_cpu_pct:
        reasons.append(f"CPU {sample.cpu_pct:.0f}% > {policy.max_cpu_pct:.0f}%")
    if sample.memory_pct > policy.max_memory_pct:
        reasons.append(f"memory {sample.memory_pct:.0f}% > {policy.max_memory_pct:.0f}%")
    if sample.db_conn_active is not None and sample.db_conn_max:
        ratio = sample.db_conn_active / sample.db_conn_max
        if ratio >= policy.max_pool_ratio:
            reasons.append(
                f"DB connections {sample.db_conn_active}/{sample.db_conn_max} ({ratio:.0%})"
            )

    status = HealthStatus.DEGRADED if reasons else HealthStatus.HEALTHY
    return HealthAssessment(status, reasons, availability, sample.ts)

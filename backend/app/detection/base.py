"""Detector interface and the data it works on.

A detector is anything with a ``name`` and a ``detect(series, policy)`` method
returning ``Finding``s. The threshold and robust z-score detectors implement
it today; a seasonal model or an isolation forest can implement it later
without touching correlation, persistence or the API.

Detectors are pure and stateless: they get the recent window for one service
and say what is wrong *now*. Persistence of anomalies ("is this the same one
as last cycle?") is the correlation planner's job, not theirs.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.detection.signals import Signal
from app.domain.enums import AnomalySeverity, ServiceTier


class SampleLike(Protocol):
    """Satisfied by both the simulator's samples and the ``MetricSample`` ORM model.

    Members are read-only properties so frozen dataclasses qualify too.
    """

    @property
    def ts(self) -> datetime: ...

    @property
    def cpu_pct(self) -> float: ...

    @property
    def memory_pct(self) -> float: ...

    @property
    def rps(self) -> float: ...

    @property
    def latency_p95_ms(self) -> float: ...

    @property
    def error_rate(self) -> float: ...

    @property
    def db_conn_active(self) -> int | None: ...

    @property
    def db_conn_max(self) -> int | None: ...

    @property
    def cache_hit_rate(self) -> float | None: ...

    @property
    def instances_healthy(self) -> int: ...

    @property
    def instances_total(self) -> int: ...


@dataclass
class ServiceSeries:
    """One service's recent samples, oldest first, one list per signal."""

    service: str
    ts: list[datetime]
    values: dict[Signal, list[float | None]]

    @property
    def last_ts(self) -> datetime | None:
        return self.ts[-1] if self.ts else None


def build_series(service: str, samples: Sequence[SampleLike]) -> ServiceSeries:
    ordered = sorted(samples, key=lambda s: s.ts)
    values: dict[Signal, list[float | None]] = {s: [] for s in Signal}
    for s in ordered:
        values[Signal.LATENCY_P95].append(s.latency_p95_ms)
        values[Signal.ERROR_RATE].append(s.error_rate)
        values[Signal.CPU].append(s.cpu_pct)
        values[Signal.MEMORY].append(s.memory_pct)
        values[Signal.RPS].append(s.rps)
        values[Signal.DB_POOL].append(
            s.db_conn_active / s.db_conn_max
            if s.db_conn_active is not None and s.db_conn_max
            else None
        )
        values[Signal.CACHE_HIT].append(s.cache_hit_rate)
        values[Signal.AVAILABILITY].append(
            s.instances_healthy / s.instances_total if s.instances_total else 0.0
        )
    return ServiceSeries(service=service, ts=[s.ts for s in ordered], values=values)


@dataclass(frozen=True)
class ServicePolicy:
    """What a service is judged against (from the catalog)."""

    name: str
    tier: ServiceTier
    slo_latency_p95_ms: float
    slo_error_rate: float


@dataclass(frozen=True)
class Finding:
    """A detector's statement that one signal of one service is abnormal right now."""

    service: str
    signal: Signal
    detector: str
    severity: AnomalySeverity
    observed_at: datetime
    value: float
    message: str
    baseline: float | None = None
    threshold: float | None = None
    score: float | None = None
    details: dict[str, float] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, Signal, str]:
        return (self.service, self.signal, self.detector)


class Detector(Protocol):
    @property
    def name(self) -> str: ...

    def detect(self, series: ServiceSeries, policy: ServicePolicy) -> list[Finding]: ...


SEVERITY_RANK = {AnomalySeverity.MINOR: 1, AnomalySeverity.MAJOR: 2, AnomalySeverity.CRITICAL: 3}

"""Threshold rules: objectives and hard capacity limits.

Each rule must hold for ``for_samples`` consecutive samples before it fires —
the same idea as Prometheus' ``for:`` clause. A single noisy sample above the
SLO is not an incident; three in a row is.

The durations are chosen against real behaviour of the fleet:

* A routine rolling deploy takes one instance out for 2 ticks, so
  "instances unhealthy" needs 4 to avoid paging on every deploy, while an
  OOM-kill (6 ticks to restart) still fires.
* "Service down" fires on the first sample. Waiting to confirm an outage
  only delays the page.

When several rules fire on the same signal (error rate ≥ 25% *and* above the
SLO), only the most severe is reported, so one signal yields one anomaly
whose severity can escalate.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.detection.base import SEVERITY_RANK, Finding, ServicePolicy, ServiceSeries
from app.detection.signals import SIGNALS, Signal
from app.domain.enums import AnomalySeverity


@dataclass(frozen=True)
class ThresholdRule:
    signal: Signal
    severity: AnomalySeverity
    for_samples: int
    threshold: Callable[[ServicePolicy], float]
    above: bool  # fire when value > threshold (True) or value < threshold (False)
    describe: str  # e.g. "above SLO"

    def breached(self, value: float, policy: ServicePolicy) -> bool:
        limit = self.threshold(policy)
        return value > limit if self.above else value < limit


DEFAULT_RULES: tuple[ThresholdRule, ...] = (
    ThresholdRule(
        Signal.AVAILABILITY,
        AnomalySeverity.CRITICAL,
        1,
        lambda p: 1e-9,
        False,
        "no healthy instances",
    ),
    ThresholdRule(
        Signal.AVAILABILITY, AnomalySeverity.MAJOR, 4, lambda p: 1.0, False, "instances unhealthy"
    ),
    ThresholdRule(
        Signal.ERROR_RATE,
        AnomalySeverity.CRITICAL,
        2,
        lambda p: 0.25,
        True,
        "most requests failing",
    ),
    ThresholdRule(
        Signal.ERROR_RATE, AnomalySeverity.MAJOR, 3, lambda p: p.slo_error_rate, True, "above SLO"
    ),
    ThresholdRule(
        Signal.LATENCY_P95,
        AnomalySeverity.MAJOR,
        3,
        lambda p: p.slo_latency_p95_ms,
        True,
        "above SLO",
    ),
    ThresholdRule(
        Signal.DB_POOL, AnomalySeverity.MAJOR, 2, lambda p: 0.9, True, "pool near exhaustion"
    ),
    ThresholdRule(Signal.CPU, AnomalySeverity.MINOR, 3, lambda p: 90.0, True, "sustained high CPU"),
    ThresholdRule(
        Signal.MEMORY, AnomalySeverity.MINOR, 3, lambda p: 90.0, True, "sustained high memory"
    ),
)


@dataclass(frozen=True)
class ThresholdDetector:
    rules: tuple[ThresholdRule, ...] = DEFAULT_RULES
    name: str = "threshold"

    def detect(self, series: ServiceSeries, policy: ServicePolicy) -> list[Finding]:
        best: dict[Signal, tuple[ThresholdRule, float]] = {}
        for rule in self.rules:
            values = series.values[rule.signal]
            recent = values[-rule.for_samples :]
            if len(recent) < rule.for_samples or any(v is None for v in recent):
                continue
            if not all(rule.breached(v, policy) for v in recent if v is not None):
                continue
            current = best.get(rule.signal)
            if current is None or SEVERITY_RANK[rule.severity] > SEVERITY_RANK[current[0].severity]:
                best[rule.signal] = (rule, recent[-1])  # type: ignore[assignment]

        findings = []
        for signal, (rule, value) in best.items():
            spec = SIGNALS[signal]
            limit = rule.threshold(policy)
            if signal is Signal.AVAILABILITY and rule.severity is AnomalySeverity.CRITICAL:
                message = f"{policy.name}: no healthy instances"
            else:
                message = (
                    f"{policy.name}: {spec.title} {spec.fmt(value)} {rule.describe} "
                    f"({spec.fmt(limit)}) for {rule.for_samples} samples"
                )
            findings.append(
                Finding(
                    service=policy.name,
                    signal=signal,
                    detector=self.name,
                    severity=rule.severity,
                    observed_at=series.ts[-1],
                    value=value,
                    threshold=limit,
                    message=message,
                )
            )
        return findings

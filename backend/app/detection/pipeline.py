"""Detection without a database: detectors + planner + an in-memory incident store.

Used by the evaluation harness and tests to replay the simulator through the
exact same detectors and correlation rules the worker runs. The in-memory
store applies a ``Plan`` with the same semantics as the Postgres executor
(``app/worker/detection.py``); an integration test runs one scenario through
both and compares outcomes.
"""

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.detection.base import Detector, Finding, SampleLike, ServicePolicy, build_series
from app.detection.correlation import (
    ActiveAnomaly,
    CorrelationConfig,
    Event,
    OpenIncident,
    Plan,
    plan,
)
from app.detection.statistical import RobustZScoreDetector
from app.detection.threshold import ThresholdDetector
from app.domain.enums import IncidentSeverity, IncidentStatus, ServiceTier

DEFAULT_DETECTORS: tuple[Detector, ...] = (ThresholdDetector(), RobustZScoreDetector())


def window_samples(detectors: Iterable[Detector]) -> int:
    return max((getattr(d, "window_samples", 10) for d in detectors), default=10)


def run_detectors(
    detectors: Iterable[Detector],
    windows: Mapping[str, Sequence[SampleLike]],
    policies: Mapping[str, ServicePolicy],
) -> list[Finding]:
    findings: list[Finding] = []
    for service, samples in sorted(windows.items()):
        if service in policies and samples:
            series = build_series(service, samples)
            for detector in detectors:
                findings.extend(detector.detect(series, policies[service]))
    return findings


@dataclass
class IncidentRecord:
    state: OpenIncident
    title: str
    events: list[tuple[datetime, Event]] = field(default_factory=list)
    resolved_at: datetime | None = None
    max_severity: IncidentSeverity = IncidentSeverity.SEV4
    signals: set[tuple[str, str, str]] = field(default_factory=set)


@dataclass
class InMemoryStore:
    incidents: dict[str, IncidentRecord] = field(default_factory=dict)
    active: dict[str, ActiveAnomaly] = field(default_factory=dict)
    _seq: int = 0

    def open_incidents(self) -> list[OpenIncident]:
        return [
            r.state
            for r in self.incidents.values()
            if r.state.status is not IncidentStatus.RESOLVED
        ]

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def apply(self, p: Plan, now: datetime) -> None:
        refs: dict[str, str] = {}
        for ni in p.new_incidents:
            iid = self._next("inc-")
            refs[ni.ref] = iid
            self.incidents[iid] = IncidentRecord(
                OpenIncident(iid, IncidentStatus.OPEN, ni.severity, set(ni.services), now, now),
                ni.title,
                max_severity=ni.severity,
            )
        for na in p.new_anomalies:
            iid = refs.get(na.incident, na.incident)
            f = na.finding
            aid = self._next("an-")
            self.active[aid] = ActiveAnomaly(aid, f.key, iid, f.severity, now, f.value)
            rec = self.incidents[iid]
            rec.state.services.add(f.service)
            rec.signals.add((f.service, f.signal.value, f.detector))
        for t in p.touched:
            a = self.active[t.anomaly_id]
            a.last_seen, a.peak, a.severity = now, t.peak, t.severity
        for r in p.resolved:
            self.active.pop(r.anomaly_id, None)
        for ref, ch in p.changes.items():
            rec = self.incidents[refs.get(ref, ref)]
            st = rec.state
            rec.events.extend((now, e) for e in ch.events)
            if ch.touched:
                st.last_activity = now
            if ch.severity:
                st.severity = ch.severity
                rec.max_severity = min(rec.max_severity, ch.severity)
            if ch.clear_signals_at:
                st.signals_cleared_at = ch.clear_signals_at
            if ch.unset_signals_cleared:
                st.signals_cleared_at = None
            if ch.auto_resolve and st.status is IncidentStatus.OPEN:
                st.status = IncidentStatus.RESOLVED
                rec.resolved_at = now


@dataclass
class DetectionPipeline:
    policies: Mapping[str, ServicePolicy]
    neighbors: Mapping[str, set[str]]
    depths: Mapping[str, int] = field(default_factory=dict)
    detectors: tuple[Detector, ...] = DEFAULT_DETECTORS
    config: CorrelationConfig = field(default_factory=CorrelationConfig)
    store: InMemoryStore = field(default_factory=InMemoryStore)
    _windows: dict[str, deque[SampleLike]] = field(default_factory=dict)

    @property
    def tiers(self) -> dict[str, ServiceTier]:
        return {name: p.tier for name, p in self.policies.items()}

    def observe(self, samples: Iterable[SampleLike], service_of: str = "service") -> None:
        size = window_samples(self.detectors)
        for s in samples:
            name = getattr(s, service_of)
            self._windows.setdefault(name, deque(maxlen=size)).append(s)

    def step(self, now: datetime) -> Plan:
        findings = run_detectors(self.detectors, self._windows, self.policies)
        p = plan(
            findings,
            list(self.store.active.values()),
            self.store.open_incidents(),
            self.neighbors,
            self.tiers,
            now,
            self.config,
            self.depths,
        )
        self.store.apply(p, now)
        return p

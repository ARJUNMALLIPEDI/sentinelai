"""Correlation: turn a cycle's findings into anomalies and incidents.

``plan()`` is a pure function. Given what the detectors found *now*, what was
already active, the open incidents and the dependency graph, it returns the
actions to apply. The worker applies them to Postgres in one transaction; the
evaluation harness applies them to memory. No I/O, so every rule here is
unit-tested directly.

Rules
-----
**Dedup.** An anomaly is keyed by ``(service, signal, detector)``. A finding
whose key is already active refreshes it (last seen, peak, severity
escalation) instead of creating a new one.

**Grouping (time + topology).** One root cause trips detectors on many
services: a slow database shows up in every caller. New findings are grouped
into connected components of the dependency graph restricted to services that
are anomalous right now. Each component joins an open incident that already
touches it, or one of its direct neighbours, and was active within
``correlation_window``. Otherwise it opens one new incident. A cascade becomes
one incident, not an alert storm.

*Tradeoff:* hub services (``postgres-primary``) connect almost everything, so
two genuinely unrelated failures that both reach the hub get merged. Grouping
on topology alone can't tell them apart; that judgement is left to the
investigation (Milestone 4).

**Hysteresis.** An anomaly resolves only after it has stopped firing for
``resolve_after``. A signal hovering at the threshold doesn't flap.

**Severity** follows the worst active anomaly and the tier of the service it
is on, and only escalates automatically. De-escalation is a human decision.

**Auto-resolve.** When all of an incident's anomalies have resolved, the time
is recorded (*signals recovered*). If the incident is still ``open`` (nobody
has acknowledged it) after ``auto_resolve_after``, the detector resolves it.
Once a human or the agent is engaged, they own closure.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.detection.base import SEVERITY_RANK, Finding
from app.detection.signals import SIGNALS, Direction, Signal
from app.domain.enums import (
    AnomalySeverity,
    IncidentEventKind,
    IncidentSeverity,
    IncidentStatus,
    ServiceTier,
)

AnomalyKey = tuple[str, Signal, str]


@dataclass(frozen=True)
class CorrelationConfig:
    resolve_after: timedelta = timedelta(seconds=30)
    correlation_window: timedelta = timedelta(minutes=10)
    auto_resolve_after: timedelta = timedelta(minutes=10)


# ---- state the planner reads ----------------------------------------------------------


@dataclass
class ActiveAnomaly:
    id: str
    key: AnomalyKey
    incident_id: str
    severity: AnomalySeverity
    last_seen: datetime
    peak: float

    @property
    def service(self) -> str:
        return self.key[0]


@dataclass
class OpenIncident:
    id: str
    status: IncidentStatus
    severity: IncidentSeverity
    services: set[str]
    opened_at: datetime
    last_activity: datetime
    signals_cleared_at: datetime | None = None


# ---- actions the planner emits ----------------------------------------------------------


@dataclass
class Event:
    kind: IncidentEventKind
    message: str
    data: dict[str, object] = field(default_factory=dict)


@dataclass
class NewIncident:
    ref: str  # placeholder id until persisted
    severity: IncidentSeverity
    title: str
    services: list[str]
    opened_at: datetime


@dataclass
class NewAnomaly:
    incident: str  # an existing incident id or a NewIncident.ref
    finding: Finding


@dataclass
class TouchAnomaly:
    anomaly_id: str
    finding: Finding
    peak: float
    severity: AnomalySeverity


@dataclass
class ResolveAnomaly:
    anomaly_id: str
    incident_id: str
    key: AnomalyKey


@dataclass
class IncidentChange:
    incident: str
    severity: IncidentSeverity | None = None
    clear_signals_at: datetime | None = None  # set signals_cleared_at
    unset_signals_cleared: bool = False
    auto_resolve: bool = False
    touched: bool = False  # had anomaly activity this cycle (bump last_activity)
    events: list[Event] = field(default_factory=list)


@dataclass
class Plan:
    new_incidents: list[NewIncident] = field(default_factory=list)
    new_anomalies: list[NewAnomaly] = field(default_factory=list)
    touched: list[TouchAnomaly] = field(default_factory=list)
    resolved: list[ResolveAnomaly] = field(default_factory=list)
    changes: dict[str, IncidentChange] = field(default_factory=dict)

    def change(self, incident: str) -> IncidentChange:
        return self.changes.setdefault(incident, IncidentChange(incident))

    @property
    def empty(self) -> bool:
        return not (
            self.new_incidents
            or self.new_anomalies
            or self.touched
            or self.resolved
            or any(c.events or c.severity or c.auto_resolve for c in self.changes.values())
        )


# ---- severity and titles ----------------------------------------------------------------

_SEVERITY_TABLE = {
    AnomalySeverity.CRITICAL: {
        ServiceTier.CRITICAL: IncidentSeverity.SEV1,
        ServiceTier.STANDARD: IncidentSeverity.SEV2,
        ServiceTier.INTERNAL: IncidentSeverity.SEV3,
    },
    AnomalySeverity.MAJOR: {
        ServiceTier.CRITICAL: IncidentSeverity.SEV2,
        ServiceTier.STANDARD: IncidentSeverity.SEV3,
        ServiceTier.INTERNAL: IncidentSeverity.SEV4,
    },
    AnomalySeverity.MINOR: {
        ServiceTier.CRITICAL: IncidentSeverity.SEV3,
        ServiceTier.STANDARD: IncidentSeverity.SEV4,
        ServiceTier.INTERNAL: IncidentSeverity.SEV4,
    },
}


def incident_severity(items: Iterable[tuple[AnomalySeverity, ServiceTier]]) -> IncidentSeverity:
    """Worst of each anomaly's severity weighted by its service's tier (SEV1 = worst)."""
    return min((_SEVERITY_TABLE[sev][tier] for sev, tier in items), default=IncidentSeverity.SEV4)


_TIER_RANK = {ServiceTier.CRITICAL: 0, ServiceTier.STANDARD: 1, ServiceTier.INTERNAL: 2}


def dependency_depths(edges: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Depth in the call graph: 0 for services that call nothing, else 1 + deepest callee."""
    calls: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for caller, callee in edges:
        calls[caller].add(callee)
        nodes |= {caller, callee}
    memo: dict[str, int] = {}

    def depth(n: str, seen: frozenset[str] = frozenset()) -> int:
        if n not in memo:
            deeper = [depth(c, seen | {n}) for c in calls.get(n, ()) if c not in seen]
            memo[n] = 1 + max(deeper) if deeper else 0
        return memo[n]

    return {n: depth(n) for n in sorted(nodes)}


def incident_title(
    findings: Sequence[Finding],
    tiers: Mapping[str, ServiceTier],
    depths: Mapping[str, int] | None = None,
) -> str:
    """Describe the most telling observation — never a cause; that's for the investigation.

    Among equally severe findings, prefer the deepest service in the call graph:
    failures propagate *upstream* to callers, so the deepest symptom is usually
    the most informative headline.
    """
    depths = depths or {}
    top = min(
        findings,
        key=lambda f: (
            -SEVERITY_RANK[f.severity],
            depths.get(f.service, 0),
            _TIER_RANK[tiers.get(f.service, ServiceTier.STANDARD)],
            f.service,
            f.signal,
        ),
    )
    if top.signal is Signal.AVAILABILITY and top.severity is AnomalySeverity.CRITICAL:
        head = f"{top.service} is down"
    else:
        kind = "anomaly" if top.severity is AnomalySeverity.MINOR else "breach"
        title = SIGNALS[top.signal].title
        head = f"{title[0].upper()}{title[1:]} {kind} on {top.service}"
    others = len({f.service for f in findings}) - 1
    return head + (f" (+{others} service{'s' if others > 1 else ''})" if others else "")


def _peak(signal: Signal, old: float, new: float) -> float:
    return min(old, new) if SIGNALS[signal].direction is Direction.DOWN else max(old, new)


def neighbors_from_edges(edges: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    """Undirected adjacency: a caller and its dependency are neighbours either way."""
    out: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        out[a].add(b)
        out[b].add(a)
    return dict(out)


def _components(services: set[str], neighbors: Mapping[str, set[str]]) -> list[set[str]]:
    """Connected components of the dependency graph restricted to ``services``."""
    remaining, out = set(services), []
    for start in sorted(services):
        if start not in remaining:
            continue
        comp, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node in comp:
                continue
            comp.add(node)
            stack.extend(n for n in neighbors.get(node, ()) if n in remaining and n not in comp)
        remaining -= comp
        out.append(comp)
    return out


# ---- the planner ----------------------------------------------------------------------------


def plan(
    findings: Sequence[Finding],
    active: Sequence[ActiveAnomaly],
    incidents: Sequence[OpenIncident],
    neighbors: Mapping[str, set[str]],
    tiers: Mapping[str, ServiceTier],
    now: datetime,
    config: CorrelationConfig | None = None,
    depths: Mapping[str, int] | None = None,
) -> Plan:
    config = config or CorrelationConfig()
    out = Plan()
    by_key = {a.key: a for a in active}
    firing: dict[AnomalyKey, Finding] = {}
    for f in findings:  # one finding per key; keep the most severe if duplicated
        cur = firing.get(f.key)
        if cur is None or SEVERITY_RANK[f.severity] > SEVERITY_RANK[cur.severity]:
            firing[f.key] = f

    # 1. Refresh anomalies that are still firing.
    for key, f in sorted(firing.items()):
        if (a := by_key.get(key)) is not None:
            severity = max(a.severity, f.severity, key=SEVERITY_RANK.__getitem__)
            out.touched.append(TouchAnomaly(a.id, f, _peak(key[1], a.peak, f.value), severity))
            out.change(a.incident_id).touched = True

    # 2. Group new findings by topology and attach them to incidents.
    new = {k: f for k, f in firing.items() if k not in by_key}
    anomalous = {k[0] for k in firing}
    # Treat currently-active-but-quiet services as anomalous too: a service whose
    # anomaly is inside its hysteresis window still links a cascade together.
    anomalous |= {a.service for a in active}
    recent = [i for i in incidents if now - i.last_activity <= config.correlation_window]

    for comp in _components(anomalous, neighbors):
        comp_new = [f for k, f in sorted(new.items()) if k[0] in comp]
        if not comp_new:
            continue
        touching = set(comp) | {n for s in comp for n in neighbors.get(s, ())}
        candidates = sorted(
            (i for i in recent if i.services & touching),
            key=lambda i: (not (i.services & comp), i.opened_at),
        )
        if candidates:
            target = candidates[0].id
            candidates[0].services |= {f.service for f in comp_new}
        else:
            target = f"new-{len(out.new_incidents) + 1}"
            out.new_incidents.append(
                NewIncident(
                    ref=target,
                    severity=incident_severity((f.severity, tiers[f.service]) for f in comp_new),
                    title=incident_title(comp_new, tiers, depths),
                    services=sorted({f.service for f in comp_new}),
                    opened_at=now,
                )
            )
            out.change(target).events.append(
                Event(
                    IncidentEventKind.OPENED,
                    f"Incident opened by detection: {comp_new[0].message}",
                )
            )
        for f in comp_new:
            out.new_anomalies.append(NewAnomaly(target, f))
            ch = out.change(target)
            ch.touched = True
            ch.events.append(
                Event(
                    IncidentEventKind.ANOMALY_DETECTED,
                    f.message,
                    {
                        "service": f.service,
                        "signal": f.signal.value,
                        "detector": f.detector,
                        "severity": f.severity.value,
                        "value": f.value,
                    },
                )
            )

    # 3. Resolve anomalies that have been quiet for longer than the hysteresis window.
    for a in active:
        if a.key not in firing and now - a.last_seen >= config.resolve_after:
            out.resolved.append(ResolveAnomaly(a.id, a.incident_id, a.key))
            out.change(a.incident_id).events.append(
                Event(
                    IncidentEventKind.ANOMALY_RESOLVED,
                    f"{a.service}: {SIGNALS[a.key[1]].title} back to normal ({a.key[2]})",
                    {"service": a.service, "signal": a.key[1].value, "detector": a.key[2]},
                )
            )

    # 4. Per-incident bookkeeping: severity, recovery, regression, auto-resolve.
    resolved_ids = {r.anomaly_id for r in out.resolved}
    remaining: dict[str, list[tuple[AnomalySeverity, ServiceTier]]] = defaultdict(list)
    touched_sev = {t.anomaly_id: t.severity for t in out.touched}
    for a in active:
        if a.id not in resolved_ids:
            sev = touched_sev.get(a.id, a.severity)
            remaining[a.incident_id].append((sev, tiers[a.service]))
    for na in out.new_anomalies:
        remaining[na.incident].append((na.finding.severity, tiers[na.finding.service]))

    quiet_min = int(config.auto_resolve_after.total_seconds() // 60)
    for inc in incidents:
        ch = out.change(inc.id)
        items = remaining.get(inc.id, [])
        if items:
            worst = incident_severity(items)
            if worst < inc.severity:  # "sev1" < "sev2": strictly more severe
                ch.severity = worst
                ch.events.append(
                    Event(
                        IncidentEventKind.SEVERITY_CHANGED,
                        f"Severity escalated {inc.severity.value.upper()} → {worst.value.upper()}",
                        {"from": inc.severity.value, "to": worst.value},
                    )
                )
            if inc.signals_cleared_at is not None:
                ch.unset_signals_cleared = True
                ch.events.append(
                    Event(
                        IncidentEventKind.SIGNALS_REGRESSED,
                        "Anomalous signals returned after recovery",
                    )
                )
        elif inc.signals_cleared_at is None:
            ch.clear_signals_at = now
            ch.events.append(
                Event(IncidentEventKind.SIGNALS_RECOVERED, "All anomalous signals have recovered")
            )
        elif (
            inc.status is IncidentStatus.OPEN
            and now - inc.signals_cleared_at >= config.auto_resolve_after
        ):
            ch.auto_resolve = True
            ch.events.append(
                Event(
                    IncidentEventKind.STATUS_CHANGED,
                    f"Auto-resolved: signals normal for {quiet_min} minutes and nobody "
                    "had acknowledged the incident",
                    {"from": inc.status.value, "to": IncidentStatus.RESOLVED.value},
                )
            )

    out.changes = {
        k: v
        for k, v in out.changes.items()
        if v.events
        or v.severity
        or v.touched
        or v.auto_resolve
        or v.clear_signals_at
        or v.unset_signals_cleared
    }
    return out

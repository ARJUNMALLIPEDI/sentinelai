"""The detection job: load recent metrics, run detectors, apply the plan.

One cycle is one transaction, guarded by a Postgres advisory lock. arq's
``unique`` cron already prevents overlapping runs in one worker; the lock also
covers two workers or a manual run racing the cron. Losing the race just means
skipping a cycle (``pg_try_advisory_xact_lock``), never double-applying.

The job reads metrics from Postgres, not from the simulator's memory. That
keeps it honest: detection sees exactly what the API, the dashboard and the
agent see, and it would work unchanged against real telemetry.

``now`` is the newest sample timestamp, not the wall clock, so a lagging
cycle evaluates the data it actually has instead of mistaking lag for silence.
"""

import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.detection.base import ServicePolicy
from app.detection.correlation import (
    ActiveAnomaly,
    CorrelationConfig,
    IncidentChange,
    OpenIncident,
    Plan,
    dependency_depths,
    neighbors_from_edges,
    plan,
)
from app.detection.pipeline import DEFAULT_DETECTORS, run_detectors, window_samples
from app.domain.enums import (
    AnomalyStatus,
    IncidentEventKind,
    IncidentStatus,
)
from app.models import (
    Anomaly,
    Incident,
    IncidentEvent,
    IncidentService,
    MetricSample,
    Service,
    ServiceDependency,
)

logger = logging.getLogger("sentinel.detection")

DETECTION_LOCK_KEY = 0x5E17_DE7C  # arbitrary, stable advisory-lock id
DETECTOR_ACTOR = "detector"
EVIDENCE_LEAD = timedelta(minutes=2)  # link logs from shortly before the incident opened

DETECTION_RUNS = Counter("sentinel_detection_runs_total", "Detection cycles", ["outcome"])
DETECTION_DURATION = Histogram(
    "sentinel_detection_duration_seconds",
    "Time for one detection cycle",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
ANOMALIES_OPENED = Counter(
    "sentinel_anomalies_opened_total", "Anomalies opened", ["detector", "signal"]
)
INCIDENTS_OPENED = Counter("sentinel_incidents_opened_total", "Incidents opened", ["severity"])
INCIDENTS_OPEN = Gauge("sentinel_incidents_open", "Incidents not yet resolved")


@dataclass
class DetectionOutcome:
    ran: bool
    plan: Plan | None = None
    incident_ids: dict[str, uuid.UUID] | None = None  # plan refs/ids -> persisted ids


async def run_detection(
    session: AsyncSession,
    tick_seconds: int,
    config: CorrelationConfig | None = None,
) -> DetectionOutcome:
    """Run one cycle inside the caller's transaction (the caller commits)."""
    config = config or CorrelationConfig()
    started = time.perf_counter()
    got_lock = (
        await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": DETECTION_LOCK_KEY}
        )
    ).scalar_one()
    if not got_lock:
        DETECTION_RUNS.labels("skipped_locked").inc()
        return DetectionOutcome(ran=False)

    now = (await session.execute(select(func.max(MetricSample.ts)))).scalar_one_or_none()
    if now is None:
        DETECTION_RUNS.labels("no_data").inc()
        return DetectionOutcome(ran=False)

    # --- catalog: policies and topology ------------------------------------------------
    services = (await session.execute(select(Service))).scalars().all()
    by_id = {s.id: s for s in services}
    ids = {s.name: s.id for s in services}
    policies = {
        s.name: ServicePolicy(s.name, s.tier, s.slo_latency_p95_ms, s.slo_error_rate)
        for s in services
    }
    edges = [
        (by_id[d.service_id].name, by_id[d.depends_on_id].name)
        for d in (await session.execute(select(ServiceDependency))).scalars().all()
    ]
    neighbors, depths = neighbors_from_edges(edges), dependency_depths(edges)

    # --- metric windows (stale services are skipped, not judged) -------------------------
    window = timedelta(seconds=tick_seconds * (window_samples(DEFAULT_DETECTORS) + 1))
    rows = (
        (
            await session.execute(
                select(MetricSample).where(MetricSample.ts > now - window).order_by(MetricSample.ts)
            )
        )
        .scalars()
        .all()
    )
    windows: dict[str, list[MetricSample]] = defaultdict(list)
    for r in rows:
        windows[by_id[r.service_id].name].append(r)
    fresh_after = now - timedelta(seconds=tick_seconds * 3)
    windows = {k: v for k, v in windows.items() if v[-1].ts >= fresh_after}

    # --- current state ------------------------------------------------------------------------
    active_rows = (
        (await session.execute(select(Anomaly).where(Anomaly.status == AnomalyStatus.ACTIVE)))
        .scalars()
        .all()
    )
    active = [
        ActiveAnomaly(
            str(a.id),
            (by_id[a.service_id].name, a.signal, a.detector),
            str(a.incident_id),
            a.severity,
            a.last_seen,
            a.peak_value,
        )
        for a in active_rows
    ]
    open_rows = (
        (await session.execute(select(Incident).where(Incident.status != IncidentStatus.RESOLVED)))
        .scalars()
        .all()
    )
    members: dict[uuid.UUID, set[str]] = defaultdict(set)
    if open_rows:
        for inc_id, svc_id in (
            await session.execute(
                select(IncidentService.incident_id, IncidentService.service_id).where(
                    IncidentService.incident_id.in_([i.id for i in open_rows])
                )
            )
        ).all():
            members[inc_id].add(by_id[svc_id].name)
    incidents = [
        OpenIncident(
            str(i.id),
            i.status,
            i.severity,
            members[i.id],
            i.opened_at,
            i.last_activity_at,
            i.signals_cleared_at,
        )
        for i in open_rows
    ]

    # --- detect, plan, apply --------------------------------------------------------------------
    findings = run_detectors(DEFAULT_DETECTORS, windows, policies)
    p = plan(
        findings,
        active,
        incidents,
        neighbors,
        {n: pol.tier for n, pol in policies.items()},
        now,
        config,
        depths,
    )
    refs = await apply_plan(session, p, now, ids)
    await link_evidence_logs(session, now)

    open_count = (
        await session.execute(
            select(func.count())
            .select_from(Incident)
            .where(Incident.status != IncidentStatus.RESOLVED)
        )
    ).scalar_one()
    INCIDENTS_OPEN.set(open_count)
    DETECTION_RUNS.labels("ok").inc()
    DETECTION_DURATION.observe(time.perf_counter() - started)
    return DetectionOutcome(ran=True, plan=p, incident_ids=refs)


async def apply_plan(
    session: AsyncSession, p: Plan, now: datetime, ids: dict[str, uuid.UUID]
) -> dict[str, uuid.UUID]:
    """Persist a plan. Mirrors ``InMemoryStore.apply`` in ``app/detection/pipeline.py``."""
    refs: dict[str, uuid.UUID] = {}
    for ni in p.new_incidents:
        inc = Incident(
            title=ni.title,
            severity=ni.severity,
            status=IncidentStatus.OPEN,
            detected_by="detector",
            created_by=DETECTOR_ACTOR,
            opened_at=now,
            last_activity_at=now,
        )
        session.add(inc)
        await session.flush()  # assigns id and number
        refs[ni.ref] = inc.id
        INCIDENTS_OPENED.labels(ni.severity.value).inc()
        logger.info(
            "incident_opened",
            extra={"ctx": {"incident": inc.ref, "severity": ni.severity.value, "title": ni.title}},
        )

    def resolve(ref: str) -> uuid.UUID:
        return refs.get(ref) or uuid.UUID(ref)

    for na in p.new_anomalies:
        f = na.finding
        incident_id = resolve(na.incident)
        session.add(
            Anomaly(
                incident_id=incident_id,
                service_id=ids[f.service],
                signal=f.signal,
                detector=f.detector,
                severity=f.severity,
                status=AnomalyStatus.ACTIVE,
                first_seen=now,
                last_seen=now,
                value=f.value,
                peak_value=f.value,
                baseline=f.baseline,
                threshold=f.threshold,
                score=f.score,
                message=f.message,
            )
        )
        await session.execute(
            insert(IncidentService)
            .values(incident_id=incident_id, service_id=ids[f.service])
            .on_conflict_do_nothing()
        )
        ANOMALIES_OPENED.labels(f.detector, f.signal.value).inc()
    await session.flush()

    for t in p.touched:
        await session.execute(
            update(Anomaly)
            .where(Anomaly.id == uuid.UUID(t.anomaly_id))
            .values(
                last_seen=now,
                value=t.finding.value,
                peak_value=t.peak,
                severity=t.severity,
                score=t.finding.score,
                message=t.finding.message,
            )
        )
    for r in p.resolved:
        await session.execute(
            update(Anomaly)
            .where(Anomaly.id == uuid.UUID(r.anomaly_id))
            .values(status=AnomalyStatus.RESOLVED, resolved_at=now)
        )

    for ref, ch in p.changes.items():
        await _apply_change(session, resolve(ref), ch, now)
    return refs


async def _apply_change(
    session: AsyncSession, incident_id: uuid.UUID, ch: IncidentChange, now: datetime
) -> None:
    events = list(ch.events)
    values: dict[str, Any] = {}
    if ch.touched:
        values["last_activity_at"] = now
    if ch.clear_signals_at:
        values["signals_cleared_at"] = ch.clear_signals_at
    if ch.unset_signals_cleared:
        values["signals_cleared_at"] = None
    if values:
        await session.execute(update(Incident).where(Incident.id == incident_id).values(**values))

    if ch.severity:
        # Compare-and-set: only escalate relative to the *current* row, so a severity a
        # human set in the meantime is never lowered by the detector.
        res = await session.execute(
            update(Incident)
            .where(Incident.id == incident_id, Incident.severity > ch.severity)
            .values(severity=ch.severity)
        )
        if res.rowcount == 0:  # type: ignore[attr-defined]
            events = [e for e in events if e.kind is not IncidentEventKind.SEVERITY_CHANGED]

    if ch.auto_resolve:
        # Only resolve if still untouched: if someone acknowledged it after the plan
        # was built, the WHERE clause fails and they keep ownership.
        res = await session.execute(
            update(Incident)
            .where(Incident.id == incident_id, Incident.status == IncidentStatus.OPEN)
            .values(status=IncidentStatus.RESOLVED, resolved_at=now)
        )
        if res.rowcount == 0:  # type: ignore[attr-defined]
            events = [e for e in events if e.kind is not IncidentEventKind.STATUS_CHANGED]

    for e in events:
        session.add(
            IncidentEvent(
                incident_id=incident_id,
                ts=now,
                kind=e.kind,
                actor=DETECTOR_ACTOR,
                message=e.message,
                data=e.data,
            )
        )


async def link_evidence_logs(session: AsyncSession, now: datetime) -> int:
    """Attach WARNING+ logs from an open incident's services to it as evidence."""
    result = await session.execute(
        text("""
        UPDATE log_entries AS l
           SET incident_id = i.id
          FROM incidents AS i
          JOIN incident_services AS s ON s.incident_id = i.id
         WHERE i.status <> 'resolved'
           AND l.incident_id IS NULL
           AND l.service_id = s.service_id
           AND l.level IN ('WARNING', 'ERROR', 'CRITICAL')
           AND l.ts >= i.opened_at - make_interval(secs => :lead)
           AND l.ts <= :now
    """),
        {"lead": EVIDENCE_LEAD.total_seconds(), "now": now},
    )
    return result.rowcount or 0  # type: ignore[attr-defined]

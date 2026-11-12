"""Incident reads and the human-driven writes (create, transition, notes)."""

import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    AnomalyStatus,
    IncidentEventKind,
    IncidentSeverity,
    IncidentStatus,
)
from app.domain.incidents import check_transition
from app.models import Anomaly, Incident, IncidentEvent, IncidentService, LogEntry, Service

_REF = re.compile(r"^(?:INC-)?(\d+)$", re.IGNORECASE)


async def get_incident(session: AsyncSession, ref: str, *, lock: bool = False) -> Incident | None:
    """Look up by UUID, ``INC-42`` or ``42``. ``lock=True`` adds SELECT … FOR UPDATE."""
    stmt = select(Incident)
    if m := _REF.match(ref.strip()):
        stmt = stmt.where(Incident.number == int(m.group(1)))
    else:
        try:
            stmt = stmt.where(Incident.id == uuid.UUID(ref))
        except ValueError:
            return None
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def services_by_incident(
    session: AsyncSession, incident_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    out: dict[uuid.UUID, list[str]] = defaultdict(list)
    if incident_ids:
        rows = await session.execute(
            select(IncidentService.incident_id, Service.name)
            .join(Service, Service.id == IncidentService.service_id)
            .where(IncidentService.incident_id.in_(incident_ids))
            .order_by(Service.name)
        )
        for iid, name in rows.all():
            out[iid].append(name)
    return out


async def active_anomaly_counts(
    session: AsyncSession, incident_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not incident_ids:
        return {}
    rows = await session.execute(
        select(Anomaly.incident_id, func.count())
        .where(Anomaly.incident_id.in_(incident_ids), Anomaly.status == AnomalyStatus.ACTIVE)
        .group_by(Anomaly.incident_id)
    )
    return {iid: n for iid, n in rows.all()}


async def list_incidents(
    session: AsyncSession,
    statuses: Sequence[IncidentStatus] | None,
    severity: IncidentSeverity | None,
    limit: int,
) -> list[Incident]:
    stmt = select(Incident)
    if statuses:
        stmt = stmt.where(Incident.status.in_(statuses))
    if severity:
        stmt = stmt.where(Incident.severity == severity)
    stmt = stmt.order_by(Incident.opened_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def anomalies_with_names(
    session: AsyncSession,
    *,
    incident_id: uuid.UUID | None = None,
    status: AnomalyStatus | None = None,
    service_id: uuid.UUID | None = None,
    limit: int = 500,
) -> list[tuple[Anomaly, str, int]]:
    stmt = (
        select(Anomaly, Service.name, Incident.number)
        .join(Service, Service.id == Anomaly.service_id)
        .join(Incident, Incident.id == Anomaly.incident_id)
    )
    if incident_id:
        stmt = stmt.where(Anomaly.incident_id == incident_id)
    if status:
        stmt = stmt.where(Anomaly.status == status)
    if service_id:
        stmt = stmt.where(Anomaly.service_id == service_id)
    stmt = stmt.order_by(Anomaly.first_seen.desc()).limit(limit)
    return [(a, name, num) for a, name, num in (await session.execute(stmt)).all()]


async def timeline(session: AsyncSession, incident_id: uuid.UUID) -> list[IncidentEvent]:
    rows = await session.execute(
        select(IncidentEvent)
        .where(IncidentEvent.incident_id == incident_id)
        .order_by(IncidentEvent.ts, IncidentEvent.id)
    )
    return list(rows.scalars().all())


async def evidence_logs(
    session: AsyncSession, incident_id: uuid.UUID, limit: int
) -> list[tuple[LogEntry, str]]:
    rows = await session.execute(
        select(LogEntry, Service.name)
        .join(Service, Service.id == LogEntry.service_id)
        .where(LogEntry.incident_id == incident_id)
        .order_by(LogEntry.ts.desc(), LogEntry.id.desc())
        .limit(limit)
    )
    return [(e, n) for e, n in rows.all()]


def add_event(
    session: AsyncSession,
    incident_id: uuid.UUID,
    now: datetime,
    kind: IncidentEventKind,
    actor: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> None:
    session.add(
        IncidentEvent(
            incident_id=incident_id,
            ts=now,
            kind=kind,
            actor=actor,
            message=message,
            data=data or {},
        )
    )


# ---- human-driven writes -----------------------------------------------------------------


async def create_manual(
    session: AsyncSession,
    *,
    title: str,
    severity: IncidentSeverity,
    service_ids: Sequence[uuid.UUID],
    summary: str | None,
    actor: str,
    now: datetime,
) -> Incident:
    inc = Incident(
        title=title,
        summary=summary,
        severity=severity,
        status=IncidentStatus.OPEN,
        detected_by="manual",
        created_by=actor,
        opened_at=now,
        last_activity_at=now,
    )
    session.add(inc)
    await session.flush()
    await session.execute(
        insert(IncidentService), [{"incident_id": inc.id, "service_id": sid} for sid in service_ids]
    )
    add_event(
        session, inc.id, now, IncidentEventKind.OPENED, actor, f"Incident opened manually: {title}"
    )
    return inc


async def transition(
    session: AsyncSession, inc: Incident, target: IncidentStatus, actor: str, now: datetime
) -> None:
    """Move ``inc`` (row-locked by the caller) to ``target``; raises InvalidTransitionError."""
    check_transition(inc.status, target)
    previous = inc.status
    inc.status = target
    inc.last_activity_at = now
    if target is not IncidentStatus.OPEN and inc.acknowledged_at is None:
        inc.acknowledged_at = now  # any engagement counts as acknowledgement
    if target is IncidentStatus.MITIGATED:
        inc.mitigated_at = now
    if target is IncidentStatus.RESOLVED:
        inc.resolved_at = now
        # Close the incident's anomalies with it. If the problem is still there,
        # the next detection cycle opens a *new* incident rather than piling fresh
        # evidence onto one a human has declared over.
        closed = await session.execute(
            update(Anomaly)
            .where(Anomaly.incident_id == inc.id, Anomaly.status == AnomalyStatus.ACTIVE)
            .values(status=AnomalyStatus.RESOLVED, resolved_at=now)
        )
        if closed.rowcount:  # type: ignore[attr-defined]
            add_event(
                session,
                inc.id,
                now,
                IncidentEventKind.NOTE,
                actor,
                f"{closed.rowcount} still-active anomal{'y' if closed.rowcount == 1 else 'ies'} "  # type: ignore[attr-defined]
                "closed with the incident",
            )
    if target is IncidentStatus.OPEN:  # reopen
        inc.resolved_at = None
    add_event(
        session,
        inc.id,
        now,
        IncidentEventKind.STATUS_CHANGED,
        actor,
        f"Status {previous.value} → {target.value}",
        {"from": previous.value, "to": target.value},
    )


def set_severity(
    inc: Incident, severity: IncidentSeverity, actor: str, now: datetime, session: AsyncSession
) -> bool:
    if severity is inc.severity:
        return False
    previous = inc.severity
    inc.severity = severity
    inc.last_activity_at = now
    add_event(
        session,
        inc.id,
        now,
        IncidentEventKind.SEVERITY_CHANGED,
        actor,
        f"Severity {previous.value.upper()} → {severity.value.upper()}",
        {"from": previous.value, "to": severity.value},
    )
    return True

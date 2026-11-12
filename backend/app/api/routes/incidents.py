from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.errors import AppError, ConflictError, NotFoundError
from app.domain.enums import AnomalyStatus, IncidentEventKind, IncidentSeverity, IncidentStatus
from app.domain.incidents import InvalidTransitionError
from app.models import Anomaly, Incident
from app.repositories import catalog
from app.repositories import incidents as repo
from app.schemas.incidents import (
    AnomalyOut,
    IncidentCreate,
    IncidentDetail,
    IncidentEventOut,
    IncidentSummary,
    IncidentUpdate,
    NoteCreate,
)
from app.schemas.telemetry import LogOut

router = APIRouter(tags=["incidents"])
Db = Annotated[AsyncSession, Depends(get_db)]


class InvalidIncidentRequest(AppError):
    status_code = 422
    code = "invalid_incident"


async def _require(db: AsyncSession, ref: str, *, lock: bool = False) -> Incident:
    inc = await repo.get_incident(db, ref, lock=lock)
    if inc is None:
        raise NotFoundError(f"incident '{ref}' not found")
    return inc


def _summary(inc: Incident, services: list[str], active: int) -> IncidentSummary:
    return IncidentSummary(
        id=inc.id,
        number=inc.number,
        ref=inc.ref,
        title=inc.title,
        status=inc.status,
        severity=inc.severity,
        detected_by=inc.detected_by,
        opened_at=inc.opened_at,
        last_activity_at=inc.last_activity_at,
        acknowledged_at=inc.acknowledged_at,
        mitigated_at=inc.mitigated_at,
        resolved_at=inc.resolved_at,
        signals_cleared_at=inc.signals_cleared_at,
        services=services,
        active_anomalies=active,
    )


def _anomaly(a: Anomaly, service: str, number: int) -> AnomalyOut:
    return AnomalyOut(
        id=a.id,
        incident_id=a.incident_id,
        incident_ref=f"INC-{number}",
        service=service,
        signal=a.signal,
        detector=a.detector,
        severity=a.severity,
        status=a.status,
        first_seen=a.first_seen,
        last_seen=a.last_seen,
        resolved_at=a.resolved_at,
        value=a.value,
        peak_value=a.peak_value,
        baseline=a.baseline,
        threshold=a.threshold,
        score=a.score,
        message=a.message,
    )


async def _detail(db: AsyncSession, inc: Incident) -> IncidentDetail:
    services = (await repo.services_by_incident(db, [inc.id])).get(inc.id, [])
    anomalies = await repo.anomalies_with_names(db, incident_id=inc.id)
    active = sum(1 for a, _, _ in anomalies if a.status is AnomalyStatus.ACTIVE)
    return IncidentDetail(
        **_summary(inc, services, active).model_dump(),
        summary=inc.summary,
        created_by=inc.created_by,
        anomalies=[_anomaly(a, s, n) for a, s, n in anomalies],
        timeline=[
            IncidentEventOut(
                id=e.id, ts=e.ts, kind=e.kind, actor=e.actor, message=e.message, data=e.data
            )
            for e in await repo.timeline(db, inc.id)
        ],
    )


@router.get("/incidents", response_model=list[IncidentSummary])
async def list_incidents(
    db: Db,
    status_: Annotated[list[IncidentStatus] | None, Query(alias="status")] = None,
    active: Annotated[bool, Query(description="Only incidents that are not resolved")] = False,
    severity: IncidentSeverity | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IncidentSummary]:
    statuses = status_ or (
        [s for s in IncidentStatus if s is not IncidentStatus.RESOLVED] if active else None
    )
    rows = await repo.list_incidents(db, statuses, severity, limit)
    ids = [i.id for i in rows]
    services = await repo.services_by_incident(db, ids)
    counts = await repo.active_anomaly_counts(db, ids)
    return [_summary(i, services.get(i.id, []), counts.get(i.id, 0)) for i in rows]


@router.post("/incidents", response_model=IncidentDetail, status_code=status.HTTP_201_CREATED)
async def create_incident(body: IncidentCreate, db: Db) -> IncidentDetail:
    known = await catalog.service_ids(db)
    unknown = sorted(set(body.services) - set(known))
    if unknown:
        raise InvalidIncidentRequest(f"unknown service(s): {', '.join(unknown)}")
    inc = await repo.create_manual(
        db,
        title=body.title,
        severity=body.severity,
        service_ids=[known[s] for s in dict.fromkeys(body.services)],
        summary=body.summary,
        actor=body.actor,
        now=datetime.now(UTC),
    )
    await db.commit()
    return await _detail(db, inc)


@router.get("/incidents/{ref}", response_model=IncidentDetail)
async def get_incident(ref: str, db: Db) -> IncidentDetail:
    return await _detail(db, await _require(db, ref))


@router.patch("/incidents/{ref}", response_model=IncidentDetail)
async def update_incident(ref: str, body: IncidentUpdate, db: Db) -> IncidentDetail:
    """Change status and/or severity. The row is locked for the duration, so two
    responders acting at once are serialised instead of overwriting each other."""
    now = datetime.now(UTC)
    inc = await _require(db, ref, lock=True)
    if body.status is not None:
        try:
            await repo.transition(db, inc, body.status, body.actor, now)
        except InvalidTransitionError as exc:
            raise ConflictError(str(exc)) from exc
    if body.severity is not None:
        repo.set_severity(inc, body.severity, body.actor, now, db)
    if body.note:
        repo.add_event(db, inc.id, now, IncidentEventKind.NOTE, body.actor, body.note)
    await db.commit()
    return await _detail(db, inc)


@router.post(
    "/incidents/{ref}/notes", response_model=IncidentEventOut, status_code=status.HTTP_201_CREATED
)
async def add_note(ref: str, body: NoteCreate, db: Db) -> IncidentEventOut:
    inc = await _require(db, ref)
    now = datetime.now(UTC)
    repo.add_event(db, inc.id, now, IncidentEventKind.NOTE, body.actor, body.message)
    await db.commit()
    event = (await repo.timeline(db, inc.id))[-1]
    return IncidentEventOut(
        id=event.id,
        ts=event.ts,
        kind=event.kind,
        actor=event.actor,
        message=event.message,
        data=event.data,
    )


@router.get("/incidents/{ref}/logs", response_model=list[LogOut])
async def incident_logs(
    ref: str, db: Db, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[LogOut]:
    """WARNING+ logs from the incident's services, linked as evidence by detection."""
    inc = await _require(db, ref)
    return [
        LogOut(
            id=e.id,
            service=name,
            ts=e.ts,
            level=e.level,
            message=e.message,
            instance=e.instance,
            trace_id=e.trace_id,
            attributes=e.attributes,
        )
        for e, name in await repo.evidence_logs(db, inc.id, limit)
    ]


@router.get("/anomalies", response_model=list[AnomalyOut])
async def list_anomalies(
    db: Db,
    status_: Annotated[AnomalyStatus | None, Query(alias="status")] = None,
    service: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AnomalyOut]:
    service_id = None
    if service:
        svc = await catalog.get_service(db, service)
        if svc is None:
            raise NotFoundError(f"service '{service}' not found")
        service_id = svc.id
    rows = await repo.anomalies_with_names(db, status=status_, service_id=service_id, limit=limit)
    return [_anomaly(a, s, n) for a, s, n in rows]

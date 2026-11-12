import uuid
from typing import Annotated

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.service import request_investigation, steps
from app.api.deps import get_app_settings, get_arq, get_db
from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.models import Incident, IncidentReportRecord, Investigation
from app.repositories import incidents as incident_repo
from app.schemas.investigations import (
    AgentStepOut,
    InvestigationCreate,
    InvestigationDetail,
    InvestigationOut,
    ReportOut,
)

router = APIRouter(tags=["investigations"])
Db = Annotated[AsyncSession, Depends(get_db)]


class AgentNotConfigured(AppError):
    status_code = 503
    code = "agent_not_configured"


async def _incident(db: AsyncSession, ref: str) -> Incident:
    inc = await incident_repo.get_incident(db, ref)
    if inc is None:
        raise NotFoundError(f"incident '{ref}' not found")
    return inc


def _out(inv: Investigation) -> InvestigationOut:
    return InvestigationOut.model_validate(inv, from_attributes=True)


@router.post(
    "/incidents/{ref}/investigations",
    response_model=InvestigationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_investigation(
    ref: str,
    db: Db,
    settings: Annotated[Settings, Depends(get_app_settings)],
    queue: Annotated[ArqRedis, Depends(get_arq)],
    body: InvestigationCreate | None = None,
) -> InvestigationOut:
    """Queue an AI investigation. 409 if one is already running for this incident."""
    if not settings.agent_configured:
        raise AgentNotConfigured(
            "ANTHROPIC_API_KEY is not set; the investigation agent is disabled"
        )
    inc = await _incident(db, ref)
    inv = await request_investigation(
        db,
        inc,
        trigger="manual",
        requested_by=(body or InvestigationCreate()).actor,
        settings=settings,
    )
    await db.commit()
    await queue.enqueue_job("investigate", str(inv.id), _job_id=f"inv-{inv.id}")
    await db.refresh(inv)
    return _out(inv)


@router.get("/incidents/{ref}/investigations", response_model=list[InvestigationOut])
async def list_investigations(ref: str, db: Db) -> list[InvestigationOut]:
    inc = await _incident(db, ref)
    rows = await db.execute(
        select(Investigation)
        .where(Investigation.incident_id == inc.id)
        .order_by(Investigation.created_at.desc())
    )
    return [_out(i) for i in rows.scalars()]


@router.get("/investigations/{investigation_id}", response_model=InvestigationDetail)
async def get_investigation(investigation_id: uuid.UUID, db: Db) -> InvestigationDetail:
    inv = await db.get(Investigation, investigation_id)
    if inv is None:
        raise NotFoundError("investigation not found")
    return InvestigationDetail(
        **_out(inv).model_dump(),
        steps=[
            AgentStepOut.model_validate(s, from_attributes=True) for s in await steps(db, inv.id)
        ],
    )


@router.get("/incidents/{ref}/report", response_model=ReportOut)
async def latest_report(ref: str, db: Db) -> ReportOut:
    """The most recent report for the incident (complete or partial)."""
    inc = await _incident(db, ref)
    rec = (
        await db.execute(
            select(IncidentReportRecord)
            .where(IncidentReportRecord.incident_id == inc.id)
            .order_by(IncidentReportRecord.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if rec is None:
        raise NotFoundError(f"{inc.ref} has no report yet")
    return ReportOut(
        incident_ref=inc.ref,
        investigation_id=rec.investigation_id,
        created_at=rec.created_at,
        partial=rec.partial,
        root_cause_status=rec.root_cause_status,
        confidence=rec.confidence,
        report=rec.report,
    )

"""Investigation lifecycle: request → run → persist.

``request_investigation`` creates a queued row (the partial unique index
rejects a second active one per incident). ``execute_investigation`` claims it
with a compare-and-set (queued → running), runs the loop, and stores the
outcome. Whatever happens inside, the row ends in a terminal state: an
exception becomes ``failed`` with the error recorded, never a row stuck in
``running``.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.llm import LLMClient, LLMResponse
from app.agent.orchestrator import AgentLimits, Outcome, Recorder, run_investigation
from app.agent.prompts import investigation_brief
from app.agent.tools import build_context
from app.core.config import Settings
from app.core.errors import ConflictError
from app.domain.enums import IncidentEventKind, IncidentStatus, InvestigationStatus
from app.domain.incidents import TRANSITIONS
from app.models import (
    AgentStep,
    Anomaly,
    Incident,
    IncidentEvent,
    IncidentReportRecord,
    Investigation,
    Service,
    ServiceDependency,
)
from app.repositories import incidents as incident_repo

logger = logging.getLogger("sentinel.agent")
AGENT_ACTOR = "agent"
MAX_STORED_OUTPUT = 20_000


def limits_from(settings: Settings) -> AgentLimits:
    return AgentLimits(
        max_tool_calls=settings.agent_max_tool_calls,
        max_turns=settings.agent_max_turns,
        max_wall_s=settings.agent_timeout_s,
    )


async def request_investigation(
    session: AsyncSession,
    incident: Incident,
    *,
    trigger: str,
    requested_by: str,
    settings: Settings,
) -> Investigation:
    """Create a queued investigation. Raises ConflictError if one is already active."""
    ref = incident.ref  # read before flushing: a rollback expires every loaded object
    inv = Investigation(
        incident_id=incident.id,
        status=InvestigationStatus.QUEUED,
        trigger=trigger,
        requested_by=requested_by,
        model=settings.anthropic_model,
        effort=settings.agent_effort,
    )
    session.add(inv)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"{ref} already has an investigation in progress") from exc
    return inv


class DbRecorder:
    """Persists each step as it happens, so progress is visible while it runs."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        investigation_id: uuid.UUID,
        echo: Recorder | None = None,
    ) -> None:
        self.sessionmaker, self.investigation_id, self.echo = sessionmaker, investigation_id, echo

    async def _add(self, step: AgentStep) -> None:
        async with self.sessionmaker() as s:
            s.add(step)
            await s.commit()

    async def llm_call(self, seq: int, response: LLMResponse, latency_ms: float) -> None:
        text = "\n".join(b.get("text", "") for b in response.content if b.get("type") == "text")
        calls = [b["name"] for b in response.content if b.get("type") == "tool_use"]
        await self._add(
            AgentStep(
                investigation_id=self.investigation_id,
                seq=seq,
                ts=datetime.now(UTC),
                kind="llm_call",
                stop_reason=response.stop_reason,
                latency_ms=latency_ms,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                data={
                    "text": text[:MAX_STORED_OUTPUT],
                    "tool_calls": calls,
                    "cache_read_tokens": response.usage.cache_read_tokens,
                },
            )
        )
        if self.echo:
            await self.echo.llm_call(seq, response, latency_ms)

    async def tool_call(
        self,
        seq: int,
        name: str,
        tool_input: Any,
        output: str,
        evidence_id: str | None,
        is_error: bool,
        latency_ms: float,
    ) -> None:
        await self._add(
            AgentStep(
                investigation_id=self.investigation_id,
                seq=seq,
                ts=datetime.now(UTC),
                kind="tool_call",
                tool_name=name,
                evidence_id=evidence_id,
                is_error=is_error,
                latency_ms=latency_ms,
                data={"input": tool_input, "output": output[:MAX_STORED_OUTPUT]},
            )
        )
        if self.echo:
            await self.echo.tool_call(
                seq, name, tool_input, output, evidence_id, is_error, latency_ms
            )


async def _brief(
    session: AsyncSession, incident: Incident, anchor: datetime, max_tool_calls: int
) -> str:
    names = {sid: n for sid, n in (await session.execute(select(Service.id, Service.name))).all()}
    anomalies = (
        (
            await session.execute(
                select(Anomaly)
                .where(Anomaly.incident_id == incident.id)
                .order_by(Anomaly.first_seen)
            )
        )
        .scalars()
        .all()
    )
    events = (
        (
            await session.execute(
                select(IncidentEvent)
                .where(IncidentEvent.incident_id == incident.id)
                .order_by(IncidentEvent.ts.desc(), IncidentEvent.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    edges = (await session.execute(select(ServiceDependency))).scalars().all()
    services = (await incident_repo.services_by_incident(session, [incident.id])).get(
        incident.id, []
    )
    return investigation_brief(
        ref=incident.ref,
        title=incident.title,
        severity=incident.severity.value,
        status=incident.status.value,
        opened_at=incident.opened_at,
        anchor=anchor,
        services=services,
        anomalies=[f"{a.first_seen:%H:%M:%S} [{a.status.value}] {a.message}" for a in anomalies],
        timeline=[
            f"{e.ts:%H:%M:%S} {e.kind.value} ({e.actor}): {e.message}" for e in reversed(events)
        ],
        topology=sorted(
            f"{names[d.service_id]} -> {names[d.depends_on_id]}"
            f"{' (async)' if d.is_async else ''} criticality={d.criticality}"
            for d in edges
        ),
        max_tool_calls=max_tool_calls,
    )


async def execute_investigation(
    sessionmaker: async_sessionmaker[AsyncSession],
    investigation_id: uuid.UUID,
    llm: LLMClient,
    settings: Settings,
    *,
    limits: AgentLimits | None = None,
    echo: Recorder | None = None,
) -> Outcome | None:
    """Run a queued investigation to a terminal state. Returns None if it wasn't claimable."""
    now = datetime.now(UTC)
    async with sessionmaker() as s:
        claimed = await s.execute(
            update(Investigation)
            .where(
                Investigation.id == investigation_id,
                Investigation.status == InvestigationStatus.QUEUED,
            )
            .values(status=InvestigationStatus.RUNNING, started_at=now)
            .returning(
                Investigation.incident_id,
                Investigation.requested_by,
                Investigation.model,
                Investigation.effort,
            )
        )
        row = claimed.first()
        if row is None:
            return None
        incident_id, requested_by, model, effort = row
        incident = await incident_repo.get_incident(s, str(incident_id), lock=True)
        assert incident is not None
        # An investigation is engagement: it moves the incident to "investigating",
        # which also stops detection from auto-resolving it underneath the agent.
        if IncidentStatus.INVESTIGATING in TRANSITIONS[incident.status]:
            await incident_repo.transition(
                s, incident, IncidentStatus.INVESTIGATING, AGENT_ACTOR, now
            )
        incident_repo.add_event(
            s,
            incident.id,
            now,
            IncidentEventKind.INVESTIGATION_STARTED,
            AGENT_ACTOR,
            f"Investigation started (requested by {requested_by}, model {model})",
            {"investigation_id": str(investigation_id)},
        )
        await s.commit()

    limits = limits or limits_from(settings)
    try:
        ctx = await build_context(sessionmaker, incident_id, settings.sim_tick_seconds)
        async with sessionmaker() as s:
            inc = await incident_repo.get_incident(s, str(incident_id))
            assert inc is not None
            brief = await _brief(s, inc, ctx.anchor, limits.max_tool_calls)
        outcome = await run_investigation(
            ctx,
            llm,
            brief,
            model=model,
            effort=effort,
            limits=limits,
            recorder=DbRecorder(sessionmaker, investigation_id, echo),
        )
    except Exception as exc:  # platform bug: record it, never leave the row running
        logger.exception(
            "investigation_crashed", extra={"ctx": {"investigation": str(investigation_id)}}
        )
        outcome = Outcome(status="failed", error=f"internal error: {type(exc).__name__}: {exc}")

    await _store_outcome(sessionmaker, investigation_id, incident_id, model, outcome)
    return outcome


async def _store_outcome(
    sessionmaker: async_sessionmaker[AsyncSession],
    investigation_id: uuid.UUID,
    incident_id: uuid.UUID,
    model: str,
    outcome: Outcome,
) -> None:
    now = datetime.now(UTC)
    status = InvestigationStatus(outcome.status)
    async with sessionmaker() as s:
        await s.execute(
            update(Investigation)
            .where(Investigation.id == investigation_id)
            .values(
                status=status,
                finished_at=now,
                error=outcome.error,
                llm_calls=outcome.llm_calls,
                tool_calls=outcome.tool_calls,
                input_tokens=outcome.usage.input_tokens,
                output_tokens=outcome.usage.output_tokens,
                cache_read_tokens=outcome.usage.cache_read_tokens,
                cache_write_tokens=outcome.usage.cache_write_tokens,
                cost_usd=outcome.usage.cost_usd(model),
            )
        )
        body = outcome.report.model_dump(mode="json") if outcome.report else outcome.partial
        if body is not None:
            rc = outcome.report.root_cause if outcome.report else None
            s.add(
                IncidentReportRecord(
                    incident_id=incident_id,
                    investigation_id=investigation_id,
                    partial=outcome.report is None,
                    report=body,
                    created_at=now,
                    root_cause_status=rc.status if rc else None,
                    confidence=outcome.report.overall_confidence.value if outcome.report else None,
                )
            )
        if outcome.report:
            rc = outcome.report.root_cause
            message = (
                f"Report ready. Root cause ({rc.status}, {rc.confidence.value} confidence): "
                f"{rc.statement}"
            )
            kind = IncidentEventKind.INVESTIGATION_COMPLETED
        elif status is InvestigationStatus.INCOMPLETE:
            message = f"Investigation incomplete ({outcome.error}); partial report stored"
            kind = IncidentEventKind.INVESTIGATION_COMPLETED
        else:
            message = f"Investigation failed: {outcome.error}"
            kind = IncidentEventKind.INVESTIGATION_FAILED
        incident_repo.add_event(
            s,
            incident_id,
            now,
            kind,
            AGENT_ACTOR,
            message[:2000],
            {
                "investigation_id": str(investigation_id),
                "status": status.value,
                "tool_calls": outcome.tool_calls,
                "llm_calls": outcome.llm_calls,
            },
        )
        await s.commit()


async def fail_stale(sessionmaker: async_sessionmaker[AsyncSession], max_age_s: float) -> int:
    """On worker start: fail investigations too old to still be running anywhere.

    Only rows older than the agent's wall-clock budget are touched, so a worker
    restart (or hot reload) never kills an investigation that is still live in
    another process, e.g. one started from the CLI.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=max_age_s + 60)
    async with sessionmaker() as s:
        res = await s.execute(
            update(Investigation)
            .where(
                Investigation.status.in_([InvestigationStatus.QUEUED, InvestigationStatus.RUNNING]),
                func.coalesce(Investigation.started_at, Investigation.created_at) < cutoff,
            )
            .values(
                status=InvestigationStatus.FAILED,
                finished_at=datetime.now(UTC),
                error="abandoned: the worker stopped before the investigation finished",
            )
        )
        await s.commit()
        return res.rowcount or 0  # type: ignore[attr-defined]


async def steps(session: AsyncSession, investigation_id: uuid.UUID) -> Sequence[AgentStep]:
    rows = await session.execute(
        select(AgentStep)
        .where(AgentStep.investigation_id == investigation_id)
        .order_by(AgentStep.seq)
    )
    return rows.scalars().all()

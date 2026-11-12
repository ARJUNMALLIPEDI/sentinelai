"""Reads and writes for metric samples, logs and deployments."""

import base64
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DeploymentKind, LogLevel
from app.models import Deployment, LogEntry, MetricSample, Service


async def insert_samples(session: AsyncSession, rows: Sequence[Mapping[str, object]]) -> None:
    """Insert metric rows (``service_id`` + signal columns). Duplicate (service, ts)
    rows are skipped, so retried ticks and overlapping backfills are harmless."""
    if rows:
        stmt = insert(MetricSample).on_conflict_do_nothing(
            constraint="uq_metric_samples_service_ts"
        )
        await session.execute(stmt, list(rows))


async def insert_deployment(
    session: AsyncSession,
    *,
    service_id: uuid.UUID,
    version: str,
    previous_version: str | None,
    kind: DeploymentKind,
    initiated_by: str,
    deployed_at: datetime,
) -> None:
    session.add(
        Deployment(
            service_id=service_id,
            version=version,
            previous_version=previous_version,
            kind=kind,
            initiated_by=initiated_by,
            deployed_at=deployed_at,
        )
    )
    await session.execute(
        update(Service).where(Service.id == service_id).values(current_version=version)
    )


async def latest_sample_ts(session: AsyncSession) -> datetime | None:
    return (await session.execute(select(func.max(MetricSample.ts)))).scalar_one_or_none()


async def metric_series(
    session: AsyncSession, service_id: uuid.UUID, start: datetime, end: datetime, limit: int
) -> list[MetricSample]:
    result = await session.execute(
        select(MetricSample)
        .where(
            MetricSample.service_id == service_id, MetricSample.ts >= start, MetricSample.ts <= end
        )
        .order_by(MetricSample.ts.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))  # newest N, returned oldest-first for charting


# ---- logs ------------------------------------------------------------------------


def encode_cursor(ts: datetime, id_: int) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{id_}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        ts, id_ = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(ts), int(id_)
    except Exception as exc:  # malformed / tampered cursor
        raise ValueError("invalid cursor") from exc


async def query_logs(
    session: AsyncSession,
    *,
    service_ids: Sequence[uuid.UUID] | None,
    levels: Sequence[LogLevel] | None,
    start: datetime,
    end: datetime,
    contains: str | None,
    cursor: tuple[datetime, int] | None,
    limit: int,
) -> list[tuple[LogEntry, str]]:
    stmt = (
        select(LogEntry, Service.name)
        .join(Service, Service.id == LogEntry.service_id)
        .where(LogEntry.ts >= start, LogEntry.ts <= end)
    )
    if service_ids:
        stmt = stmt.where(LogEntry.service_id.in_(service_ids))
    if levels:
        stmt = stmt.where(LogEntry.level.in_(levels))
    if contains:
        # Escape LIKE wildcards so user text is matched literally. A substring
        # scan is fine at this volume; at scale this becomes a pg_trgm GIN
        # index or a dedicated log store.
        escaped = contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(LogEntry.message.ilike(f"%{escaped}%", escape="\\"))
    if cursor:
        stmt = stmt.where(tuple_(LogEntry.ts, LogEntry.id) < tuple_(cursor[0], cursor[1]))
    stmt = stmt.order_by(LogEntry.ts.desc(), LogEntry.id.desc()).limit(limit)
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


# ---- deployments -----------------------------------------------------------------


async def list_deployments(
    session: AsyncSession, service_id: uuid.UUID | None, since: datetime, limit: int
) -> list[tuple[Deployment, str]]:
    stmt = (
        select(Deployment, Service.name)
        .join(Service, Service.id == Deployment.service_id)
        .where(Deployment.deployed_at >= since)
    )
    if service_id:
        stmt = stmt.where(Deployment.service_id == service_id)
    stmt = stmt.order_by(Deployment.deployed_at.desc()).limit(limit)
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


# ---- retention ---------------------------------------------------------------------


async def prune_before(session: AsyncSession, cutoff: datetime) -> dict[str, int]:
    """Delete telemetry older than ``cutoff``.

    Kept regardless of age: deployments (tiny, and investigations look back
    further than the metrics window) and logs linked to an incident (evidence).
    """
    samples = await session.execute(delete(MetricSample).where(MetricSample.ts < cutoff))
    logs = await session.execute(
        delete(LogEntry).where(LogEntry.ts < cutoff, LogEntry.incident_id.is_(None))
    )
    return {
        "metric_samples": samples.rowcount or 0,  # type: ignore[attr-defined]
        "log_entries": logs.rowcount or 0,  # type: ignore[attr-defined]
    }

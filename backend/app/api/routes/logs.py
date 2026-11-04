from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.errors import AppError
from app.domain.enums import LogLevel
from app.logs.ingest import ingest_logs
from app.repositories import catalog
from app.repositories import telemetry as repo
from app.schemas.telemetry import LogBatchIn, LogBatchResult, LogOut, LogPage

router = APIRouter(prefix="/logs", tags=["logs"])

Db = Annotated[AsyncSession, Depends(get_db)]


class InvalidQueryError(AppError):
    status_code = 422
    code = "invalid_query"


@router.get("", response_model=LogPage)
async def search_logs(
    db: Db,
    service: Annotated[
        list[str] | None, Query(description="Repeat to filter several services")
    ] = None,
    level: Annotated[list[LogLevel] | None, Query()] = None,
    q: Annotated[
        str | None, Query(max_length=200, description="Case-insensitive substring")
    ] = None,
    minutes: Annotated[int, Query(ge=1, le=24 * 60)] = 60,
    until: Annotated[datetime | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> LogPage:
    ids = None
    if service:
        known = await catalog.service_ids(db)
        unknown = sorted(set(service) - set(known))
        if unknown:
            raise InvalidQueryError(f"unknown service(s): {', '.join(unknown)}")
        ids = [known[s] for s in service]
    try:
        after = repo.decode_cursor(cursor) if cursor else None
    except ValueError as exc:
        raise InvalidQueryError(str(exc)) from exc

    end = until or datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    # Fetch one extra row to know whether another page exists.
    rows = await repo.query_logs(
        db,
        service_ids=ids,
        levels=level,
        start=end - timedelta(minutes=minutes),
        end=end,
        contains=q,
        cursor=after,
        limit=limit + 1,
    )
    page, more = rows[:limit], len(rows) > limit
    items = [
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
        for e, name in page
    ]
    next_cursor = repo.encode_cursor(page[-1][0].ts, page[-1][0].id) if more and page else None
    return LogPage(items=items, next_cursor=next_cursor)


@router.post("/batch", response_model=LogBatchResult, status_code=status.HTTP_202_ACCEPTED)
async def ingest_batch(batch: LogBatchIn, db: Db) -> LogBatchResult:
    """Accept a batch of log records. Valid records are stored even if others are rejected."""
    result = await ingest_logs(db, batch.entries, await catalog.service_ids(db), datetime.now(UTC))
    await db.commit()
    return LogBatchResult(accepted=result.accepted, rejected=result.rejected)

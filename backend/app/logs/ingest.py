"""Log ingestion: the single path by which log records enter storage.

Both the simulator (in the worker) and the ``POST /logs/batch`` endpoint call
``ingest_logs``, so validation, normalisation and rejection rules are the same
no matter where a log came from.

A batch is accepted *partially*: one bad record (unknown service, clock far in
the future) is rejected with its index and reason while the rest are stored.
Dropping a whole batch because of one record is how log pipelines silently lose
the evidence you need during an incident.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LogEntry
from app.schemas.telemetry import LogIn, LogRejection

MAX_CLOCK_SKEW = timedelta(minutes=5)


@dataclass
class IngestResult:
    accepted: int = 0
    rejected: list[LogRejection] = field(default_factory=list)


async def ingest_logs(
    session: AsyncSession,
    records: Sequence[LogIn],
    service_ids: Mapping[str, UUID],
    now: datetime,
) -> IngestResult:
    """Validate and insert ``records``. The caller owns the transaction (commit)."""
    result = IngestResult()
    rows: list[dict[str, object]] = []
    for i, record in enumerate(records):
        service_id = service_ids.get(record.service)
        if service_id is None:
            result.rejected.append(
                LogRejection(index=i, reason=f"unknown service '{record.service}'")
            )
            continue
        if record.ts > now + MAX_CLOCK_SKEW:
            result.rejected.append(LogRejection(index=i, reason="timestamp is in the future"))
            continue
        rows.append(
            {
                "service_id": service_id,
                "ts": record.ts,
                "level": record.level,
                "message": record.message,
                "instance": record.instance,
                "trace_id": record.trace_id,
                "attributes": record.attributes,
            }
        )
    if rows:
        await session.execute(insert(LogEntry), rows)
    result.accepted = len(rows)
    return result

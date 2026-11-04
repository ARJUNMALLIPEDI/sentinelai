from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import DeploymentKind, LogLevel

MAX_MESSAGE_CHARS = 8192
MAX_ATTRIBUTE_KEYS = 32
TRUNCATION_SUFFIX = " …[truncated]"


class MetricPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    cpu_pct: float
    memory_pct: float
    rps: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    error_rate: float
    net_in_kbps: float
    net_out_kbps: float
    db_conn_active: int | None
    db_conn_max: int | None
    cache_hit_rate: float | None
    instances_healthy: int
    instances_total: int


class MetricSnapshot(MetricPoint):
    """Latest sample for one service, as cached in Redis by the worker."""

    service: str


class MetricSeries(BaseModel):
    service: str
    start: datetime
    end: datetime
    points: list[MetricPoint]


class LogIn(BaseModel):
    """A log record entering the system, from the simulator or the HTTP batch endpoint."""

    service: str = Field(min_length=1, max_length=64)
    ts: AwareDatetime  # naive timestamps are ambiguous across hosts; reject them
    level: LogLevel
    message: str = Field(min_length=1)
    instance: str | None = Field(default=None, max_length=96)
    trace_id: str | None = Field(default=None, max_length=64)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _truncate_message(cls, v: str) -> str:
        # Oversized messages (stack dumps, payloads) are truncated rather than
        # rejected: losing the tail beats losing the whole error.
        if len(v) > MAX_MESSAGE_CHARS:
            return v[: MAX_MESSAGE_CHARS - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX
        return v

    @field_validator("attributes")
    @classmethod
    def _bound_attributes(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > MAX_ATTRIBUTE_KEYS:
            raise ValueError(f"at most {MAX_ATTRIBUTE_KEYS} attributes")
        return v


class LogBatchIn(BaseModel):
    entries: list[LogIn] = Field(min_length=1, max_length=1000)


class LogRejection(BaseModel):
    index: int
    reason: str


class LogBatchResult(BaseModel):
    accepted: int
    rejected: list[LogRejection]


class LogOut(BaseModel):
    id: int
    service: str
    ts: datetime
    level: LogLevel
    message: str
    instance: str | None
    trace_id: str | None
    attributes: dict[str, Any]


class LogPage(BaseModel):
    items: list[LogOut]
    # Opaque keyset cursor; pass back as ``cursor`` for the next (older) page.
    # Keyed on (ts, id), not id alone: backfilled logs get new ids with old
    # timestamps, so id order is not time order.
    next_cursor: str | None


class DeploymentOut(BaseModel):
    id: UUID
    service: str
    version: str
    previous_version: str | None
    kind: DeploymentKind
    initiated_by: str
    deployed_at: datetime

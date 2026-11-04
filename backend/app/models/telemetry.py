"""Time-series telemetry: metric samples, logs and deployments.

Metric samples are stored *wide* — one row per service per tick with a typed
column per signal — rather than *narrow* (service, metric_name, value) rows.

* Wide: one insert per tick, "latest snapshot" is a single-row read, columns
  are typed and nullable where a signal doesn't apply. Adding a signal is a
  migration.
* Narrow: arbitrary metrics without schema changes, but ~14× the rows and every
  dashboard query has to pivot.

The signal set is fixed and known, so wide wins. ``(service_id, ts)`` is
unique, which both indexes the dominant query ("this service over this
window") and makes inserts idempotent: a retried tick or an overlapping
backfill is a no-op instead of a duplicate.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import str_enum
from app.domain.enums import DeploymentKind, LogLevel


class MetricSample(Base):
    __tablename__ = "metric_samples"
    __table_args__ = (UniqueConstraint("service_id", "ts", name="uq_metric_samples_service_ts"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cpu_pct: Mapped[float] = mapped_column(Float, nullable=False)
    memory_pct: Mapped[float] = mapped_column(Float, nullable=False)
    rps: Mapped[float] = mapped_column(Float, nullable=False)
    latency_p50_ms: Mapped[float] = mapped_column(Float, nullable=False)
    latency_p95_ms: Mapped[float] = mapped_column(Float, nullable=False)
    latency_p99_ms: Mapped[float] = mapped_column(Float, nullable=False)
    error_rate: Mapped[float] = mapped_column(Float, nullable=False)
    net_in_kbps: Mapped[float] = mapped_column(Float, nullable=False)
    net_out_kbps: Mapped[float] = mapped_column(Float, nullable=False)
    db_conn_active: Mapped[int | None] = mapped_column(Integer)
    db_conn_max: Mapped[int | None] = mapped_column(Integer)
    cache_hit_rate: Mapped[float | None] = mapped_column(Float)
    instances_healthy: Mapped[int] = mapped_column(Integer, nullable=False)
    instances_total: Mapped[int] = mapped_column(Integer, nullable=False)


class LogEntry(Base):
    __tablename__ = "log_entries"
    __table_args__ = (
        Index("ix_log_entries_service_ts", "service_id", "ts"),
        Index("ix_log_entries_ts", "ts"),
        Index("ix_log_entries_level_ts", "level", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    level: Mapped[LogLevel] = mapped_column(str_enum(LogLevel, "log_level", 8), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    instance: Mapped[str | None] = mapped_column(String(96))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (Index("ix_deployments_service_deployed_at", "service_id", "deployed_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_version: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[DeploymentKind] = mapped_column(
        str_enum(DeploymentKind, "deployment_kind"), nullable=False
    )
    initiated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

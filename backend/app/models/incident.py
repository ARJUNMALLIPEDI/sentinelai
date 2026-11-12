"""Incidents, the anomalies that make them up, and their append-only timeline.

``anomalies`` carries a *partial* unique index on (service, signal, detector)
WHERE status = 'active'. The planner already dedups, but the database is the
last line of defence: even a bug or a second detection process can't create
two active anomalies for the same thing.
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
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import str_enum
from app.domain.enums import (
    AnomalySeverity,
    AnomalyStatus,
    IncidentEventKind,
    IncidentSeverity,
    IncidentStatus,
    Signal,
)


class Incident(TimestampMixin, Base):
    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_status_opened_at", "status", "opened_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Human-friendly reference (INC-42); the UUID stays the stable key.
    number: Mapped[int] = mapped_column(Integer, Identity(), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[IncidentStatus] = mapped_column(
        str_enum(IncidentStatus, "incident_status"), nullable=False, default=IncidentStatus.OPEN
    )
    severity: Mapped[IncidentSeverity] = mapped_column(
        str_enum(IncidentSeverity, "incident_severity", 8), nullable=False
    )
    detected_by: Mapped[str] = mapped_column(String(32), nullable=False)  # detector | manual
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mitigated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signals_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def ref(self) -> str:
        return f"INC-{self.number}"


class IncidentService(Base):
    """Services involved in an incident (from its anomalies, or named on a manual incident)."""

    __tablename__ = "incident_services"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Anomaly(Base):
    __tablename__ = "anomalies"
    __table_args__ = (
        Index(
            "uq_anomalies_active_key",
            "service_id",
            "signal",
            "detector",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_anomalies_incident_id", "incident_id"),
        Index("ix_anomalies_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    signal: Mapped[Signal] = mapped_column(str_enum(Signal, "anomaly_signal", 32), nullable=False)
    detector: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[AnomalySeverity] = mapped_column(
        str_enum(AnomalySeverity, "anomaly_severity", 8), nullable=False
    )
    status: Mapped[AnomalyStatus] = mapped_column(
        str_enum(AnomalyStatus, "anomaly_status", 8), nullable=False, default=AnomalyStatus.ACTIVE
    )
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value: Mapped[float] = mapped_column(Float, nullable=False)  # latest observed
    peak_value: Mapped[float] = mapped_column(Float, nullable=False)  # worst observed
    baseline: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str] = mapped_column(Text, nullable=False)


class IncidentEvent(Base):
    """Append-only timeline. Rows are never updated or deleted by the application."""

    __tablename__ = "incident_events"
    __table_args__ = (Index("ix_incident_events_incident_ts", "incident_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[IncidentEventKind] = mapped_column(
        str_enum(IncidentEventKind, "incident_event_kind", 24), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

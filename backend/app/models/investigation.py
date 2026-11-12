"""Investigations, every step the agent took, and the reports they produced.

``agent_steps`` is the audit trail: each model call (stop reason, tokens,
latency, any text it wrote) and each tool call (input, output, evidence ID,
error flag). An investigation can be reconstructed — and argued with — after
the fact.

At most one investigation per incident can be queued or running, enforced by
a partial unique index rather than application checks.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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
from app.domain.enums import InvestigationStatus


class Investigation(TimestampMixin, Base):
    __tablename__ = "investigations"
    __table_args__ = (
        Index(
            "uq_investigations_one_active",
            "incident_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_investigations_incident_id", "incident_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[InvestigationStatus] = mapped_column(
        str_enum(InvestigationStatus, "investigation_status"),
        nullable=False,
        default=InvestigationStatus.QUEUED,
    )
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)  # manual | auto
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    effort: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (Index("ix_agent_steps_investigation_seq", "investigation_id", "seq"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # llm_call | tool_call
    tool_name: Mapped[str | None] = mapped_column(String(64))
    evidence_id: Mapped[str | None] = mapped_column(String(16))
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class IncidentReportRecord(Base):
    """A stored report. ``partial`` reports come from investigations that ran out
    of budget: they list the evidence gathered, not conclusions."""

    __tablename__ = "incident_reports"
    __table_args__ = (Index("ix_incident_reports_incident_created", "incident_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    partial: Mapped[bool] = mapped_column(Boolean, nullable=False)
    root_cause_status: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[str | None] = mapped_column(String(8))
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

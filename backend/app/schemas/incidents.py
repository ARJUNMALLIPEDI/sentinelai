from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.domain.enums import (
    AnomalySeverity,
    AnomalyStatus,
    IncidentEventKind,
    IncidentSeverity,
    IncidentStatus,
    Signal,
)

# There is no authentication yet (planned with the approval workflow), so callers
# name themselves. Constrained so it is safe to store and display.
Actor = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[\w .@-]+$")]


class IncidentSummary(BaseModel):
    id: UUID
    number: int
    ref: str
    title: str
    status: IncidentStatus
    severity: IncidentSeverity
    detected_by: str
    opened_at: datetime
    last_activity_at: datetime
    acknowledged_at: datetime | None
    mitigated_at: datetime | None
    resolved_at: datetime | None
    signals_cleared_at: datetime | None
    services: list[str]
    active_anomalies: int


class AnomalyOut(BaseModel):
    id: UUID
    incident_id: UUID
    incident_ref: str
    service: str
    signal: Signal
    detector: str
    severity: AnomalySeverity
    status: AnomalyStatus
    first_seen: datetime
    last_seen: datetime
    resolved_at: datetime | None
    value: float
    peak_value: float
    baseline: float | None
    threshold: float | None
    score: float | None
    message: str


class IncidentEventOut(BaseModel):
    id: int
    ts: datetime
    kind: IncidentEventKind
    actor: str
    message: str
    data: dict[str, Any]


class IncidentDetail(IncidentSummary):
    summary: str | None
    created_by: str
    anomalies: list[AnomalyOut]
    timeline: list[IncidentEventOut]


class IncidentCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    severity: IncidentSeverity
    services: list[str] = Field(min_length=1, max_length=20)
    summary: str | None = Field(default=None, max_length=4000)
    actor: Actor = "operator"


class IncidentUpdate(BaseModel):
    status: IncidentStatus | None = None
    severity: IncidentSeverity | None = None
    note: str | None = Field(default=None, min_length=1, max_length=4000)
    actor: Actor = "operator"

    @model_validator(mode="after")
    def _something_to_do(self) -> "IncidentUpdate":
        if self.status is None and self.severity is None and self.note is None:
            raise ValueError("provide at least one of status, severity or note")
        return self


class NoteCreate(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    actor: Actor = "operator"

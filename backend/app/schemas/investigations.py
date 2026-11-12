from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.domain.enums import InvestigationStatus
from app.schemas.incidents import Actor


class InvestigationCreate(BaseModel):
    actor: Actor = "operator"


class InvestigationOut(BaseModel):
    id: UUID
    incident_id: UUID
    status: InvestigationStatus
    trigger: str
    requested_by: str
    model: str
    effort: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    llm_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float | None


class AgentStepOut(BaseModel):
    seq: int
    ts: datetime
    kind: str
    tool_name: str | None
    evidence_id: str | None
    is_error: bool
    stop_reason: str | None
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    data: dict[str, Any]


class InvestigationDetail(InvestigationOut):
    steps: list[AgentStepOut]


class ReportOut(BaseModel):
    incident_ref: str
    investigation_id: UUID
    created_at: datetime
    partial: bool
    root_cause_status: str | None
    confidence: str | None
    report: dict[str, Any]

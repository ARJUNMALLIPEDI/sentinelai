from datetime import datetime

from pydantic import BaseModel, Field

from app.simulation.faults import FaultType


class FaultTypeOut(BaseModel):
    type: FaultType
    title: str
    description: str
    default_duration_s: int | None
    applicable_services: list[str]


class FaultCreate(BaseModel):
    service: str
    type: FaultType
    intensity: float = Field(default=0.8, ge=0.1, le=1.0)
    duration_s: int | None = Field(
        default=None,
        ge=30,
        le=86_400,
        description="Omit for the fault type's default; bad deployments persist until cleared.",
    )


class FaultAccepted(BaseModel):
    fault_id: str
    status: str = "queued"


class ActiveFault(BaseModel):
    id: str
    type: FaultType
    service: str
    intensity: float
    started_at: datetime
    expires_at: datetime | None


class SimulationStatus(BaseModel):
    running: bool
    last_tick: datetime | None
    tick_seconds: int
    active_faults: list[ActiveFault]

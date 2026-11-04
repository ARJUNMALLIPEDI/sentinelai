from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import ServiceKind, ServiceTier
from app.monitoring.health import HealthAssessment, HealthStatus
from app.schemas.telemetry import MetricSnapshot


class HealthOut(BaseModel):
    status: HealthStatus
    reasons: list[str]
    availability: float | None
    observed_at: datetime | None

    @classmethod
    def from_assessment(cls, a: HealthAssessment) -> "HealthOut":
        return cls(
            status=a.status,
            reasons=a.reasons,
            availability=a.availability,
            observed_at=a.observed_at,
        )


class DependencyOut(BaseModel):
    service: str
    calls_per_request: float
    criticality: float
    is_async: bool


class ServiceSummary(BaseModel):
    name: str
    kind: ServiceKind
    tier: ServiceTier
    description: str | None
    desired_instances: int
    current_version: str | None
    slo_latency_p95_ms: float
    slo_error_rate: float
    health: HealthOut
    latest: MetricSnapshot | None


class ServiceDetail(ServiceSummary):
    dependencies: list[DependencyOut]
    dependents: list[str]


class FleetOverview(BaseModel):
    generated_at: datetime
    counts: dict[HealthStatus, int]
    services: list[ServiceSummary]

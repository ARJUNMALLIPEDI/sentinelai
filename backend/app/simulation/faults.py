"""Failure scenarios that can be injected into the simulated fleet.

Each fault changes *one* service's local behaviour. Everything that happens to
other services (latency, errors, connection pressure) emerges from the
dependency graph in the engine. That's deliberate: if faults directly edited
upstream metrics, the "root cause" would be baked into the data and the
investigation agent would have nothing real to reason about.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.domain.enums import ServiceKind
from app.simulation.topology import ServiceSpec


class FaultType(StrEnum):
    CPU_SPIKE = "cpu_spike"
    MEMORY_LEAK = "memory_leak"
    DB_CONNECTION_LEAK = "db_connection_leak"
    SLOW_QUERIES = "slow_queries"
    LATENCY_REGRESSION = "latency_regression"
    ERROR_BURST = "error_burst"
    SERVICE_OUTAGE = "service_outage"
    CACHE_DEGRADATION = "cache_degradation"


@dataclass(frozen=True)
class FaultDefinition:
    type: FaultType
    title: str
    description: str
    default_duration_s: int | None  # None = persists until cleared (like a bad deploy)
    kinds: frozenset[ServiceKind]
    requires_db_pool: bool = False

    def applies_to(self, spec: ServiceSpec) -> bool:
        if spec.kind not in self.kinds:
            return False
        return not self.requires_db_pool or spec.uses_db_pool


_API = frozenset({ServiceKind.API})

FAULT_CATALOG: dict[FaultType, FaultDefinition] = {
    d.type: d
    for d in (
        FaultDefinition(
            FaultType.CPU_SPIKE,
            "CPU saturation",
            "A hot code path or noisy neighbour burns CPU, shrinking request capacity.",
            600,
            frozenset({ServiceKind.API, ServiceKind.DATABASE}),
        ),
        FaultDefinition(
            FaultType.MEMORY_LEAK,
            "Memory leak",
            "Heap grows steadily until containers are OOM-killed and restart.",
            1800,
            _API,
        ),
        FaultDefinition(
            FaultType.DB_CONNECTION_LEAK,
            "DB connection leak",
            "Connections are not returned to the pool; it exhausts and requests time out.",
            900,
            _API,
            requires_db_pool=True,
        ),
        FaultDefinition(
            FaultType.SLOW_QUERIES,
            "Slow queries",
            "Missing index or lock contention slows queries; callers hold connections longer.",
            900,
            frozenset({ServiceKind.DATABASE}),
        ),
        FaultDefinition(
            FaultType.LATENCY_REGRESSION,
            "Bad deployment",
            "A new version ships with a latency regression. Persists until cleared (rollback).",
            None,
            _API,
        ),
        FaultDefinition(
            FaultType.ERROR_BURST,
            "Error burst",
            "A code path throws unhandled exceptions for a fraction of requests.",
            600,
            _API,
        ),
        FaultDefinition(
            FaultType.SERVICE_OUTAGE,
            "Service outage",
            "All instances fail health checks.",
            300,
            frozenset({ServiceKind.API, ServiceKind.DATABASE, ServiceKind.CACHE}),
        ),
        FaultDefinition(
            FaultType.CACHE_DEGRADATION,
            "Cache degradation",
            "Cache hit rate collapses (eviction storm); callers fall back to the database.",
            900,
            frozenset({ServiceKind.CACHE}),
        ),
    )
}


class InvalidFaultError(ValueError):
    pass


@dataclass
class Fault:
    id: str
    type: FaultType
    service: str
    intensity: float  # 0.1 .. 1.0
    started_at: datetime
    expires_at: datetime | None

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": self.type.value,
            "service": self.service,
            "intensity": self.intensity,
            "started_at": self.started_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def validate_fault(
    fault_type: FaultType, spec: ServiceSpec | None, intensity: float, duration_s: int | None
) -> None:
    """Shared by the API (to reject bad requests synchronously) and the engine."""
    if spec is None:
        raise InvalidFaultError("unknown service")
    definition = FAULT_CATALOG[fault_type]
    if not definition.applies_to(spec):
        raise InvalidFaultError(
            f"{fault_type.value} cannot be applied to {spec.name} ({spec.kind})"
        )
    if not 0.1 <= intensity <= 1.0:
        raise InvalidFaultError("intensity must be between 0.1 and 1.0")
    if duration_s is not None and not 30 <= duration_s <= 86_400:
        raise InvalidFaultError("duration_s must be between 30 and 86400")


def new_fault(
    fault_type: FaultType,
    spec: ServiceSpec,
    intensity: float,
    duration_s: int | None,
    now: datetime,
    fault_id: str | None = None,
) -> Fault:
    validate_fault(fault_type, spec, intensity, duration_s)
    if duration_s is None:
        duration_s = FAULT_CATALOG[fault_type].default_duration_s
    return Fault(
        id=fault_id or f"flt_{uuid.uuid4().hex[:12]}",
        type=fault_type,
        service=spec.name,
        intensity=intensity,
        started_at=now,
        expires_at=now + timedelta(seconds=duration_s) if duration_s else None,
    )

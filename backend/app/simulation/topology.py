"""The simulated fleet: services, their capacities, SLOs and dependency graph.

This module is pure data. The engine reads it to generate signals, the worker
seeds it into the ``services`` catalog, and the API uses it to validate fault
injection. Nothing here performs I/O.

Traffic is *derived*, not configured per service: only edge services receive
external traffic (``external_rps``); every other service's load is the sum of
its callers' traffic times ``calls_per_request``. That is what makes an outage
upstream reduce load downstream, and a scale-up of a caller increase it.

Baseline peak utilisation with the numbers below (diurnal factor 1.0):

    api-gateway 0.35   auth 0.37   orders 0.44   inventory 0.63
    payments 0.35      notification 0.31   postgres 0.20   redis 0.03
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.domain.enums import ServiceKind, ServiceTier

__all__ = ["FLEET", "Dependency", "ServiceKind", "ServiceSpec", "ServiceTier", "TopologyError"]


@dataclass(frozen=True)
class Dependency:
    target: str
    calls_per_request: float
    # Probability that a failed downstream call fails the caller's request.
    # 1.0 = hard dependency; 0.0 = fire-and-forget (errors are swallowed).
    criticality: float
    # Async calls (queues, fire-and-forget) add no latency to the caller.
    is_async: bool = False


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    kind: ServiceKind
    tier: ServiceTier
    description: str
    instances: int
    capacity_rps_per_instance: float
    base_latency_ms: float  # service time at low load (no queueing, no dependencies)
    base_cpu_pct: float  # idle CPU
    base_memory_pct: float
    base_error_rate: float
    slo_latency_p95_ms: float
    slo_error_rate: float
    request_kb: float
    response_kb: float
    external_rps: float = 0.0  # traffic from outside the fleet (edge services only)
    db_pool_per_instance: int | None = None  # app services with a Postgres pool
    # Connection hold time per request at baseline DB latency. This is the whole
    # transaction, not just query time (orders holds its connection across the
    # payments call — a common anti-pattern that makes it sensitive to DB slowness).
    db_hold_ms: float = 0.0
    max_connections: int | None = None  # databases only
    routes: tuple[str, ...] = ()
    dependencies: tuple[Dependency, ...] = field(default_factory=tuple)

    @property
    def uses_db_pool(self) -> bool:
        return self.db_pool_per_instance is not None


FLEET: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        name="api-gateway",
        kind=ServiceKind.API,
        tier=ServiceTier.CRITICAL,
        description="Edge gateway: routing, auth checks, rate limiting.",
        instances=3,
        capacity_rps_per_instance=400,
        base_latency_ms=4,
        base_cpu_pct=12,
        base_memory_pct=38,
        base_error_rate=0.001,
        slo_latency_p95_ms=450,
        slo_error_rate=0.02,
        request_kb=2.0,
        response_kb=6.0,
        external_rps=420,
        routes=("GET /api/catalog", "POST /api/orders", "GET /api/orders/{id}", "POST /api/login"),
        dependencies=(
            Dependency("auth-service", 0.4, 1.0),
            Dependency("orders-service", 0.25, 1.0),
            Dependency("inventory-service", 0.35, 1.0),
        ),
    ),
    ServiceSpec(
        name="auth-service",
        kind=ServiceKind.API,
        tier=ServiceTier.CRITICAL,
        description="Issues and validates session tokens.",
        instances=3,
        capacity_rps_per_instance=150,
        base_latency_ms=18,
        base_cpu_pct=10,
        base_memory_pct=45,
        base_error_rate=0.002,
        slo_latency_p95_ms=150,
        slo_error_rate=0.01,
        request_kb=1.0,
        response_kb=1.5,
        db_pool_per_instance=10,
        db_hold_ms=25,
        routes=("POST /token", "GET /token/validate", "POST /logout"),
        dependencies=(
            # A cache failure falls back to Postgres, so it rarely fails the request.
            Dependency("redis-cache", 1.0, 0.3),
            Dependency("postgres-primary", 0.2, 1.0),
        ),
    ),
    ServiceSpec(
        name="orders-service",
        kind=ServiceKind.API,
        tier=ServiceTier.CRITICAL,
        description="Order creation and lifecycle.",
        instances=3,
        capacity_rps_per_instance=80,
        base_latency_ms=35,
        base_cpu_pct=14,
        base_memory_pct=52,
        base_error_rate=0.003,
        slo_latency_p95_ms=400,
        slo_error_rate=0.02,
        request_kb=3.0,
        response_kb=4.0,
        db_pool_per_instance=20,
        db_hold_ms=150,
        routes=("POST /orders", "GET /orders/{id}", "GET /orders?customer={id}"),
        dependencies=(
            Dependency("postgres-primary", 3.0, 1.0),
            Dependency("payments-service", 0.4, 1.0),
            Dependency("inventory-service", 1.0, 0.9),
            Dependency("notification-service", 0.5, 0.0, is_async=True),
        ),
    ),
    ServiceSpec(
        name="inventory-service",
        kind=ServiceKind.API,
        tier=ServiceTier.STANDARD,
        description="Stock levels and reservations.",
        instances=2,
        capacity_rps_per_instance=200,
        base_latency_ms=22,
        base_cpu_pct=11,
        base_memory_pct=48,
        base_error_rate=0.002,
        slo_latency_p95_ms=250,
        slo_error_rate=0.02,
        request_kb=1.0,
        response_kb=3.0,
        db_pool_per_instance=15,
        db_hold_ms=40,
        routes=("GET /stock/{sku}", "POST /reservations", "DELETE /reservations/{id}"),
        dependencies=(
            Dependency("postgres-primary", 1.5, 1.0),
            Dependency("redis-cache", 2.0, 0.2),
        ),
    ),
    ServiceSpec(
        name="payments-service",
        kind=ServiceKind.API,
        tier=ServiceTier.CRITICAL,
        description="Payment authorisation via an external provider.",
        instances=2,
        capacity_rps_per_instance=60,
        base_latency_ms=85,
        base_cpu_pct=9,
        base_memory_pct=44,
        base_error_rate=0.004,
        slo_latency_p95_ms=600,
        slo_error_rate=0.01,
        request_kb=2.0,
        response_kb=1.0,
        db_pool_per_instance=10,
        db_hold_ms=120,
        routes=("POST /payments/authorize", "POST /payments/capture", "POST /refunds"),
        dependencies=(Dependency("postgres-primary", 2.0, 1.0),),
    ),
    ServiceSpec(
        name="notification-service",
        kind=ServiceKind.API,
        tier=ServiceTier.INTERNAL,
        description="Email and push notifications (async consumer).",
        instances=2,
        capacity_rps_per_instance=100,
        base_latency_ms=40,
        base_cpu_pct=7,
        base_memory_pct=36,
        base_error_rate=0.005,
        slo_latency_p95_ms=2000,
        slo_error_rate=0.05,
        request_kb=2.0,
        response_kb=0.5,
        external_rps=10,
        routes=("consume order.created", "consume order.shipped", "POST /notify"),
    ),
    ServiceSpec(
        name="postgres-primary",
        kind=ServiceKind.DATABASE,
        tier=ServiceTier.CRITICAL,
        description="Primary PostgreSQL database.",
        instances=1,
        capacity_rps_per_instance=4000,
        base_latency_ms=4,
        base_cpu_pct=15,
        base_memory_pct=60,
        base_error_rate=0.0002,
        slo_latency_p95_ms=50,
        slo_error_rate=0.005,
        request_kb=0.5,
        response_kb=2.0,
        max_connections=300,
    ),
    ServiceSpec(
        name="redis-cache",
        kind=ServiceKind.CACHE,
        tier=ServiceTier.STANDARD,
        description="Shared Redis cache (sessions, stock levels).",
        instances=1,
        capacity_rps_per_instance=20000,
        base_latency_ms=0.6,
        base_cpu_pct=6,
        base_memory_pct=42,
        base_error_rate=0.0001,
        slo_latency_p95_ms=5,
        slo_error_rate=0.005,
        request_kb=0.2,
        response_kb=0.8,
    ),
)

BASE_CACHE_HIT_RATE = 0.95


class TopologyError(ValueError):
    pass


def index_by_name(specs: Iterable[ServiceSpec]) -> dict[str, ServiceSpec]:
    by_name: dict[str, ServiceSpec] = {}
    for spec in specs:
        if spec.name in by_name:
            raise TopologyError(f"duplicate service name: {spec.name}")
        by_name[spec.name] = spec
    return by_name


def dependencies_first(specs: Mapping[str, ServiceSpec]) -> list[str]:
    """Topological order with every service after the services it calls.

    The engine computes latency and errors in this order (leaves first) and
    traffic in the reverse order (callers first). Raises on unknown targets and
    cycles — a cycle would make "caller first" undefined.
    """
    order: list[str] = []
    state: dict[str, str] = {}  # "visiting" | "done"

    def visit(name: str, path: tuple[str, ...]) -> None:
        if state.get(name) == "done":
            return
        if state.get(name) == "visiting":
            raise TopologyError(f"dependency cycle: {' -> '.join((*path, name))}")
        state[name] = "visiting"
        for dep in specs[name].dependencies:
            if dep.target not in specs:
                raise TopologyError(f"{name} depends on unknown service {dep.target}")
            visit(dep.target, (*path, name))
        state[name] = "done"
        order.append(name)

    for name in specs:  # mapping order keeps the result deterministic
        visit(name, ())
    return order


def dependents_of(specs: Mapping[str, ServiceSpec], target: str) -> list[str]:
    return [s.name for s in specs.values() if any(d.target == target for d in s.dependencies)]

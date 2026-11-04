"""Deterministic simulation of a small distributed system.

``SimulationEngine.tick(now)`` advances the fleet by one interval and returns
the metrics, logs and deployment events produced in that interval. It never
does I/O; the worker persists the result. Given the same seed and the same
sequence of ticks and fault injections, the output is identical, which is what
makes the detectors and the agent testable against known scenarios.

Signal model (per service, per tick)
------------------------------------
1. **Traffic** flows callers-first. Edge services get ``external_rps`` times a
   diurnal curve; every other service gets its callers' *admitted* traffic
   times ``calls_per_request``. A caller that is down or shedding load sends
   less downstream.
2. **Latency** is computed dependencies-first. Own response time follows the
   M/M/1 queueing result ``R = S / (1 - ρ)``: latency stays flat at low
   utilisation and climbs steeply as ρ approaches 1. Synchronous dependency
   latency is added on top (async calls add none). Percentiles are combined
   approximately — real percentiles don't add — which is fine for a simulator.
3. **DB connections** use Little's law: connections in use = request rate ×
   connection hold time. Slow queries lengthen hold time, so a slow database
   shows up as pool pressure in *every* caller. When demand exceeds the pool,
   requests wait, latency rises toward the acquire timeout and a share fail.
4. **Errors** combine a service's own failures with failed dependency calls,
   weighted by each dependency's criticality:
   ``p = 1 - (1 - own) · Π (1 - err_dep · criticality)^calls``.
5. **Noise** is AR(1) (``x_t = φ·x_{t-1} + √(1-φ²)·σ·ε``), so values drift
   smoothly instead of jittering independently like white noise — real
   metrics are autocorrelated, and detectors must cope with that.

Logs are generated *from* the computed metrics, so every number in a log line
matches the metric sample for the same tick. The investigation agent can then
cross-check evidence instead of seeing contradictory sources.
"""

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.simulation.faults import Fault, FaultType, InvalidFaultError, new_fault
from app.simulation.topology import (
    BASE_CACHE_HIT_RATE,
    FLEET,
    ServiceKind,
    ServiceSpec,
    dependencies_first,
    index_by_name,
)

DB_SERVICE = "postgres-primary"
CACHE_SERVICE = "redis-cache"

POOL_ACQUIRE_TIMEOUT_MS = 5000.0
DEP_CALL_TIMEOUT_MS = 3000.0
OOM_THRESHOLD_PCT = 97.0
RESTART_SECONDS = 30
ROUTINE_DEPLOYS_PER_SERVICE_PER_DAY = 4.0
ERROR_LOG_SAMPLE_RATE = 0.02  # log ~2% of failed requests, capped per tick
MAX_ERROR_LOGS_PER_TICK = 4


# --------------------------------------------------------------------------- outputs


@dataclass(frozen=True)
class MetricSample:
    service: str
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


@dataclass(frozen=True)
class LogRecord:
    service: str
    ts: datetime
    level: str  # INFO | WARNING | ERROR | CRITICAL
    message: str
    instance: str | None = None
    trace_id: str | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class DeploymentEvent:
    service: str
    ts: datetime
    version: str
    previous_version: str
    kind: str  # "deploy" | "rollback"
    initiated_by: str


@dataclass
class TickResult:
    ts: datetime
    samples: list[MetricSample]
    logs: list[LogRecord]
    deployments: list[DeploymentEvent]


# --------------------------------------------------------------------------- internals


class _Noise:
    """AR(1) noise per (service, signal) key with unit variance, scaled by sigma."""

    def __init__(self, rng: random.Random, phi: float = 0.85) -> None:
        self._rng = rng
        self._phi = phi
        self._scale = math.sqrt(1 - phi * phi)
        self._state: dict[tuple[str, str], float] = {}

    def __call__(self, service: str, signal: str, sigma: float) -> float:
        key = (service, signal)
        x = self._phi * self._state.get(key, 0.0) + self._scale * self._rng.gauss(0.0, 1.0)
        self._state[key] = x
        return x * sigma


@dataclass
class _ServiceState:
    desired: int
    version: tuple[int, int, int]
    gc_phase: float
    restarting: dict[int, int] = field(default_factory=dict)  # instance index -> ticks left
    leak_pct: float = 0.0
    leaked_conns: float = 0.0
    bad_deploy_previous: tuple[int, int, int] | None = None

    def healthy_indices(self) -> list[int]:
        return [i for i in range(1, self.desired + 1) if i not in self.restarting]


@dataclass
class _Computed:
    """Everything derived for one service in one tick; logs are rendered from this."""

    spec: ServiceSpec
    offered_rps: float
    healthy: int
    util: float
    shed: float
    p50: float
    p95: float
    p99: float
    error_rate: float
    cpu: float
    memory: float
    own_error_parts: dict[str, float]
    dep_error_parts: dict[str, float]
    pool_active: int | None = None
    pool_max: int | None = None
    pool_waiting: int = 0
    cache_hit_rate: float | None = None
    query_factor: float = 1.0
    oom_killed: str | None = None
    db_conn_total: int | None = None


def _fmt_version(v: tuple[int, int, int]) -> str:
    return f"v{v[0]}.{v[1]}.{v[2]}"


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- engine


class SimulationEngine:
    def __init__(
        self,
        specs: Sequence[ServiceSpec] = FLEET,
        seed: int = 42,
        tick_seconds: int = 5,
    ) -> None:
        if tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        self.specs = index_by_name(specs)
        self.tick_seconds = tick_seconds
        self._order = dependencies_first(self.specs)
        self._callers: dict[str, list[tuple[str, float]]] = {n: [] for n in self.specs}
        for spec in self.specs.values():
            for dep in spec.dependencies:
                self._callers[dep.target].append((spec.name, dep.calls_per_request))

        self._rng = random.Random(seed)
        self._noise = _Noise(self._rng)
        self._state: dict[str, _ServiceState] = {
            s.name: _ServiceState(
                desired=s.instances,
                version=(1, self._rng.randint(2, 9), self._rng.randint(0, 20)),
                gc_phase=self._rng.random() * 2 * math.pi,
            )
            for s in self.specs.values()
        }
        self._faults: dict[str, Fault] = {}
        self._pending_deploys: list[tuple[str, str, str, str, str]] = []
        self._pending_logs: list[tuple[str, str, str]] = []  # (service, level, message)
        self._db_conn_prev = 0

    # ---- public API ------------------------------------------------------------

    @property
    def active_faults(self) -> list[Fault]:
        return list(self._faults.values())

    def version_of(self, service: str) -> str:
        return _fmt_version(self._state[service].version)

    def inject_fault(
        self,
        fault_type: FaultType,
        service: str,
        now: datetime,
        intensity: float = 0.8,
        duration_s: int | None = None,
        fault_id: str | None = None,
    ) -> Fault:
        spec = self.specs.get(service)
        if spec is None:
            raise InvalidFaultError(f"unknown service: {service}")
        if any(f.service == service and f.type == fault_type for f in self._faults.values()):
            raise InvalidFaultError(f"{fault_type.value} is already active on {service}")
        fault = new_fault(fault_type, spec, intensity, duration_s, now, fault_id)
        self._faults[fault.id] = fault
        if fault_type is FaultType.LATENCY_REGRESSION:
            # A bad deploy looks exactly like a routine one in the deployment log.
            # The regression is only visible in the metrics that follow it.
            st = self._state[service]
            st.bad_deploy_previous = st.version
            st.version = (st.version[0], st.version[1], st.version[2] + 1)
            self._pending_deploys.append(
                (
                    service,
                    _fmt_version(st.version),
                    _fmt_version(st.bad_deploy_previous),
                    "deploy",
                    "ci-pipeline",
                )
            )
        return fault

    def clear_fault(self, fault_id: str, actor: str = "simulation") -> Fault:
        fault = self._faults.pop(fault_id, None)
        if fault is None:
            raise KeyError(fault_id)
        self._on_fault_ended(fault, actor)
        return fault

    def tick(self, now: datetime) -> TickResult:
        logs: list[LogRecord] = []
        deployments: list[DeploymentEvent] = []

        for fault in [f for f in self._faults.values() if f.expired(now)]:
            del self._faults[fault.id]
            self._on_fault_ended(fault, "simulation")

        self._advance_restarts(now, logs)
        self._routine_deploys()
        for service, version, previous, kind, actor in self._pending_deploys:
            deployments.append(DeploymentEvent(service, now, version, previous, kind, actor))
            verb = "Rollback" if kind == "rollback" else "Deployment"
            logs.append(
                LogRecord(
                    service,
                    now,
                    "INFO",
                    f"{verb} completed: {previous} -> {version} (by {actor})",
                    attributes={"version": version, "previous_version": previous},
                )
            )
        self._pending_deploys.clear()
        for service, level, message in self._pending_logs:
            logs.append(LogRecord(service, now, level, message))
        self._pending_logs.clear()

        faults = self._faults_by_service()
        admitted = self._traffic_pass(now, faults)
        computed: dict[str, _Computed] = {}
        for name in self._order:
            computed[name] = self._compute(name, admitted, computed, faults)

        # Postgres connection count is the sum of its callers' pools. It's known only
        # after callers are computed, so the "too many connections" effect uses the
        # previous tick's total — a one-tick lag, like a real exporter scrape.
        db_total = sum(c.pool_active or 0 for c in computed.values()) + 5
        computed[DB_SERVICE].db_conn_total = db_total
        self._db_conn_prev = db_total

        samples = [self._to_sample(c, now) for c in computed.values()]
        for c in computed.values():
            logs.extend(self._render_logs(c, now))
        return TickResult(ts=now, samples=samples, logs=logs, deployments=deployments)

    # ---- fault lifecycle ---------------------------------------------------------

    def _faults_by_service(self) -> dict[str, dict[FaultType, float]]:
        out: dict[str, dict[FaultType, float]] = {}
        for f in self._faults.values():
            out.setdefault(f.service, {})[f.type] = f.intensity
        return out

    def _on_fault_ended(self, fault: Fault, actor: str) -> None:
        st = self._state[fault.service]
        if fault.type is FaultType.LATENCY_REGRESSION and st.bad_deploy_previous:
            bad = st.version
            st.version = st.bad_deploy_previous
            st.bad_deploy_previous = None
            self._pending_deploys.append(
                (fault.service, _fmt_version(st.version), _fmt_version(bad), "rollback", actor)
            )
        elif fault.type is FaultType.MEMORY_LEAK:
            st.leak_pct = 0.0
        elif fault.type is FaultType.DB_CONNECTION_LEAK:
            st.leaked_conns = 0.0
        elif fault.type is FaultType.SERVICE_OUTAGE:
            self._pending_logs.append(
                (
                    fault.service,
                    "INFO",
                    f"Health checks passing: {st.desired}/{st.desired} instances healthy",
                )
            )

    def _advance_restarts(self, now: datetime, logs: list[LogRecord]) -> None:
        for name, st in self._state.items():
            for idx in list(st.restarting):
                st.restarting[idx] -= 1
                if st.restarting[idx] <= 0:
                    del st.restarting[idx]
                    logs.append(
                        LogRecord(
                            name,
                            now,
                            "INFO",
                            f"Instance {name}-{idx} started and passed readiness probe",
                            instance=f"{name}-{idx}",
                        )
                    )

    def _routine_deploys(self) -> None:
        p = self.tick_seconds * ROUTINE_DEPLOYS_PER_SERVICE_PER_DAY / 86_400
        for spec in self.specs.values():
            st = self._state[spec.name]
            roll = self._rng.random()
            if spec.kind is not ServiceKind.API or st.bad_deploy_previous is not None or roll >= p:
                continue
            previous = st.version
            st.version = (previous[0], previous[1], previous[2] + 1)
            self._pending_deploys.append(
                (
                    spec.name,
                    _fmt_version(st.version),
                    _fmt_version(previous),
                    "deploy",
                    "ci-pipeline",
                )
            )
            # Rolling restart: one instance is briefly out of rotation.
            if st.desired > 1 and not st.restarting:
                st.restarting[st.desired] = max(1, 10 // self.tick_seconds)

    # ---- pass 1: traffic (callers first) -----------------------------------------

    def _diurnal(self, now: datetime) -> float:
        hour = now.hour + now.minute / 60
        # Peak ~18:00 UTC (late morning in the US), trough ~06:00 UTC.
        return 0.7 + 0.3 * math.sin(2 * math.pi * (hour - 12) / 24)

    def _capacity(self, name: str, faults: dict[str, dict[FaultType, float]]) -> float:
        spec = self.specs[name]
        cpu = faults.get(name, {}).get(FaultType.CPU_SPIKE, 0.0)
        return self._healthy(name, faults) * spec.capacity_rps_per_instance / (1 + 1.5 * cpu)

    def _healthy(self, name: str, faults: dict[str, dict[FaultType, float]]) -> int:
        if FaultType.SERVICE_OUTAGE in faults.get(name, {}):
            return 0
        return len(self._state[name].healthy_indices())

    def _cache_hit_rate(self, faults: dict[str, dict[FaultType, float]]) -> float:
        degraded = faults.get(CACHE_SERVICE, {}).get(FaultType.CACHE_DEGRADATION, 0.0)
        if self._healthy(CACHE_SERVICE, faults) == 0:
            return 0.0
        return _clamp(BASE_CACHE_HIT_RATE - 0.7 * degraded, 0.0, 1.0)

    def _extra_db_calls(self, spec: ServiceSpec, hit_rate: float) -> float:
        """Cache misses beyond baseline become database reads."""
        cache_calls = sum(
            d.calls_per_request for d in spec.dependencies if d.target == CACHE_SERVICE
        )
        has_db = any(d.target == DB_SERVICE for d in spec.dependencies)
        return cache_calls * max(0.0, BASE_CACHE_HIT_RATE - hit_rate) if has_db else 0.0

    def _traffic_pass(
        self, now: datetime, faults: dict[str, dict[FaultType, float]]
    ) -> dict[str, tuple[float, float]]:
        """Return {service: (offered_rps, admitted_rps)}."""
        diurnal = self._diurnal(now)
        hit_rate = self._cache_hit_rate(faults)
        out: dict[str, tuple[float, float]] = {}
        for name in reversed(self._order):
            spec = self.specs[name]
            offered = spec.external_rps * diurnal * (1 + self._noise(name, "ext_rps", 0.04))
            for caller, calls in self._callers[name]:
                extra = (
                    self._extra_db_calls(self.specs[caller], hit_rate)
                    if name == DB_SERVICE
                    else 0.0
                )
                offered += out[caller][1] * (calls + extra)
            capacity = self._capacity(name, faults)
            admitted = min(offered, capacity) if capacity > 0 else 0.0
            out[name] = (max(offered, 0.0), max(admitted, 0.0))
        return out

    # ---- pass 2: latency, pools, errors (dependencies first) ----------------------

    def _compute(
        self,
        name: str,
        traffic: dict[str, tuple[float, float]],
        done: dict[str, _Computed],
        faults: dict[str, dict[FaultType, float]],
    ) -> _Computed:
        spec, st = self.specs[name], self._state[name]
        f = faults.get(name, {})
        offered, admitted = traffic[name]
        healthy = self._healthy(name, faults)
        capacity = self._capacity(name, faults)
        rho = offered / capacity if capacity > 0 else 0.0
        util = min(rho, 1.0)
        shed = max(0.0, 1 - 1 / rho) if rho > 1 else 0.0

        # Own service time, then M/M/1 response time.
        latency_factor = 1 + 3.0 * f.get(FaultType.LATENCY_REGRESSION, 0.0)
        latency_factor *= 1 + 1.0 * f.get(FaultType.CPU_SPIKE, 0.0)
        query_factor = 1 + 8.0 * f.get(FaultType.SLOW_QUERIES, 0.0)
        if spec.kind is ServiceKind.DATABASE:
            latency_factor *= query_factor
        if spec.kind is ServiceKind.CACHE:
            latency_factor *= 1 + 2.0 * f.get(FaultType.CACHE_DEGRADATION, 0.0)
        service_time = spec.base_latency_ms * latency_factor
        own = service_time / (1 - min(util, 0.95))

        dep_p50 = dep_p95 = 0.0
        dep_errors: dict[str, float] = {}
        survive = 1.0
        for dep in spec.dependencies:
            d = done[dep.target]
            calls = dep.calls_per_request
            if not dep.is_async:
                if d.healthy == 0:
                    dep_p50 += calls * 1.0  # connection refused: fails fast
                    dep_p95 += calls * 2.0
                else:
                    dep_p50 += calls * d.p50
                    dep_p95 += calls * min(d.p95, DEP_CALL_TIMEOUT_MS) * 0.6
            fail = d.error_rate * dep.criticality
            if fail > 0:
                # calls < 1 means only that fraction of requests touch the dependency;
                # calls >= 1 means each request makes several independent calls.
                part = calls * fail if calls < 1 else 1 - (1 - fail) ** calls
                dep_errors[dep.target] = part
                survive *= 1 - part

        # Cache misses become extra DB reads on the request path.
        hit_rate = self._cache_hit_rate(faults)
        extra_db = self._extra_db_calls(spec, hit_rate)
        if extra_db and DB_SERVICE in done:
            dep_p50 += extra_db * done[DB_SERVICE].p50
            dep_p95 += extra_db * done[DB_SERVICE].p95 * 0.6

        # Connection pool: Little's law (in use = arrival rate × hold time).
        pool_active = pool_max = None
        pool_waiting = 0
        wait_ms = pool_err = 0.0
        if spec.uses_db_pool and spec.db_pool_per_instance is not None:
            db = done[DB_SERVICE]
            db_calls = sum(d.calls_per_request for d in spec.dependencies if d.target == DB_SERVICE)
            # Roughly half of the hold time is query time, which scales with DB latency.
            db_ratio = db.p50 / self.specs[DB_SERVICE].base_latency_ms if db.healthy else 1.0
            hold_ms = spec.db_hold_ms * (0.5 + 0.5 * db_ratio)
            if db_calls:
                hold_ms *= (db_calls + extra_db) / db_calls
            leak = f.get(FaultType.DB_CONNECTION_LEAK, 0.0)
            pool_size = spec.db_pool_per_instance * healthy
            if leak and healthy:
                st.leaked_conns = min(pool_size, st.leaked_conns + leak * pool_size * 0.04)
            demand = (
                admitted * hold_ms / 1000 * (1 + self._noise(name, "pool", 0.05)) + st.leaked_conns
            )
            pool_max = pool_size
            if pool_size > 0:
                saturation = demand / pool_size
                pool_active = round(min(demand, pool_size))
                if saturation > 1:
                    pool_waiting = round(demand - pool_size)
                    wait_ms = min(POOL_ACQUIRE_TIMEOUT_MS, (saturation - 1) * 900)
                    pool_err = min(0.95, (1 - 1 / saturation) * 0.9)
            else:
                pool_active = 0

        burst = 0.25 * f.get(FaultType.ERROR_BURST, 0.0)
        base_err = spec.base_error_rate * max(0.0, 1 + self._noise(name, "err", 0.35))
        too_many = 0.0
        if (
            name == DB_SERVICE
            and spec.max_connections
            and self._db_conn_prev > spec.max_connections
        ):
            too_many = 1 - spec.max_connections / self._db_conn_prev
        own_parts = {
            "base": base_err,
            "burst": burst,
            "shed": shed,
            "pool": pool_err,
            "too_many_connections": too_many,
        }
        own_survive = math.prod(1 - p for p in own_parts.values())
        error_rate = 1.0 if healthy == 0 else _clamp(1 - own_survive * survive, 0.0, 1.0)

        # Latency percentiles (approximate composition) with multiplicative noise.
        lat_noise = 1 + self._noise(name, "lat", 0.06)
        p50 = (own * 0.8 + dep_p50 + wait_ms * 0.4) * lat_noise
        p95 = (own * 1.9 + dep_p95 + wait_ms) * lat_noise
        p99 = p95 * 1.6
        if pool_waiting:
            p99 = max(p99, POOL_ACQUIRE_TIMEOUT_MS)

        cpu = (
            spec.base_cpu_pct
            + 65 * util
            + 45 * f.get(FaultType.CPU_SPIKE, 0.0)
            + 35 * f.get(FaultType.SLOW_QUERIES, 0.0)
            + self._noise(name, "cpu", 2.5)
        )

        # Memory: GC sawtooth + noise + leak; OOM-kill an instance at the limit.
        st.gc_phase = (st.gc_phase + 2 * math.pi * self.tick_seconds / 600) % (2 * math.pi)
        st.leak_pct += f.get(FaultType.MEMORY_LEAK, 0.0) * 0.6 * self.tick_seconds / 5
        memory = (
            spec.base_memory_pct
            + 3 * (st.gc_phase / math.pi - 1)
            + st.leak_pct
            + self._noise(name, "mem", 1.0)
        )
        oom_killed = None
        if memory >= OOM_THRESHOLD_PCT and healthy > 0:
            idx = st.healthy_indices()[-1]
            st.restarting[idx] = max(1, RESTART_SECONDS // self.tick_seconds)
            st.leak_pct = 0.0  # the restarted container starts with a fresh heap
            oom_killed = f"{name}-{idx}"
            memory = min(memory, 99.5)

        if healthy == 0:
            cpu = memory = 0.0
            p50 = p95 = p99 = 0.0

        return _Computed(
            spec=spec,
            offered_rps=offered,
            healthy=healthy,
            util=util,
            shed=shed,
            p50=max(p50, 0.1) if healthy else 0.0,
            p95=max(p95, 0.2) if healthy else 0.0,
            p99=max(p99, 0.3) if healthy else 0.0,
            error_rate=error_rate,
            cpu=_clamp(cpu, 0.0, 100.0),
            memory=_clamp(memory, 0.0, 100.0),
            own_error_parts=own_parts,
            dep_error_parts=dep_errors,
            pool_active=pool_active,
            pool_max=pool_max,
            pool_waiting=pool_waiting,
            cache_hit_rate=hit_rate if spec.kind is ServiceKind.CACHE else None,
            query_factor=query_factor if spec.kind is ServiceKind.DATABASE else 1.0,
            oom_killed=oom_killed,
        )

    # ---- outputs -------------------------------------------------------------------

    def _to_sample(self, c: _Computed, now: datetime) -> MetricSample:
        spec = c.spec
        if spec.kind is ServiceKind.DATABASE:
            conn_active, conn_max = c.db_conn_total, spec.max_connections
        else:
            conn_active, conn_max = c.pool_active, c.pool_max
        served = c.offered_rps * (1 - c.error_rate)
        return MetricSample(
            service=spec.name,
            ts=now,
            cpu_pct=round(c.cpu, 2),
            memory_pct=round(c.memory, 2),
            rps=round(c.offered_rps, 2),
            latency_p50_ms=round(c.p50, 2),
            latency_p95_ms=round(c.p95, 2),
            latency_p99_ms=round(c.p99, 2),
            error_rate=round(c.error_rate, 5),
            net_in_kbps=round(c.offered_rps * spec.request_kb * 8, 2),
            net_out_kbps=round(served * spec.response_kb * 8, 2),
            db_conn_active=conn_active,
            db_conn_max=conn_max,
            cache_hit_rate=round(c.cache_hit_rate, 4) if c.cache_hit_rate is not None else None,
            instances_healthy=c.healthy,
            instances_total=self._state[spec.name].desired,
        )

    def _trace_id(self) -> str:
        return f"{self._rng.getrandbits(64):016x}"

    def _render_logs(self, c: _Computed, now: datetime) -> list[LogRecord]:
        spec, st = c.spec, self._state[c.spec.name]
        name = spec.name
        rng = self._rng
        out: list[LogRecord] = []
        healthy_idx = st.healthy_indices()

        def log(level: str, message: str, attrs: dict[str, object], trace: bool = False) -> None:
            instance = f"{name}-{rng.choice(healthy_idx)}" if healthy_idx and c.healthy else None
            out.append(
                LogRecord(
                    name, now, level, message, instance, self._trace_id() if trace else None, attrs
                )
            )

        if c.oom_killed:
            out.append(
                LogRecord(
                    name,
                    now,
                    "CRITICAL",
                    f"Container {c.oom_killed} OOMKilled (memory {c.memory:.0f}% of limit); "
                    f"restarting",
                    instance=c.oom_killed,
                    attributes={"memory_pct": round(c.memory, 1), "reason": "OOMKilled"},
                )
            )

        if c.healthy == 0:
            out.append(
                LogRecord(
                    name,
                    now,
                    "CRITICAL",
                    f"Health check failed: 0/{st.desired} instances healthy",
                    attributes={"instances_healthy": 0, "instances_total": st.desired},
                )
            )
            return out

        # Successful request logs (sampled).
        for _ in range(rng.choice((0, 1, 1, 2))):
            if not spec.routes:
                break
            route = rng.choice(spec.routes)
            ms = round(c.p50 * rng.uniform(0.6, 1.4), 1)
            status = 201 if route.startswith("POST") else 200
            log(
                "INFO",
                f"{route} {status} {ms}ms",
                {"route": route, "status": status, "latency_ms": ms},
                trace=True,
            )

        # Error logs, sampled in proportion to failed requests and attributed to a cause.
        expected = c.error_rate * c.offered_rps * self.tick_seconds * ERROR_LOG_SAMPLE_RATE
        n_errors = min(MAX_ERROR_LOGS_PER_TICK, int(expected) + (rng.random() < expected % 1))
        causes = {f"own:{k}": v for k, v in c.own_error_parts.items() if v > 0}
        causes.update({f"dep:{k}": v for k, v in c.dep_error_parts.items() if v > 0})
        for _ in range(n_errors):
            if not causes:
                break
            cause = rng.choices(list(causes), weights=list(causes.values()))[0]
            route = rng.choice(spec.routes) if spec.routes else "query"
            out.append(
                self._error_log(
                    c,
                    cause,
                    route,
                    now,
                    log_instance=(f"{name}-{rng.choice(healthy_idx)}" if healthy_idx else None),
                )
            )

        # Warnings for sustained conditions (emitted on ~half the ticks they hold).
        def warn(cond: bool, message: str, **attrs: object) -> None:
            if cond and rng.random() < 0.5:
                log("WARNING", message, dict(attrs))

        if c.pool_active is not None and c.pool_max:
            warn(
                c.pool_waiting == 0 and c.pool_active >= 0.8 * c.pool_max,
                f"DB connection pool usage high: {c.pool_active}/{c.pool_max} connections in use",
                pool_active=c.pool_active,
                pool_max=c.pool_max,
            )
        warn(
            c.p95 > spec.slo_latency_p95_ms,
            f"Slow requests: p95 latency {c.p95:.0f}ms exceeds SLO {spec.slo_latency_p95_ms:.0f}ms",
            latency_p95_ms=round(c.p95, 1),
            slo_ms=spec.slo_latency_p95_ms,
        )
        warn(
            c.memory >= 85 and not c.oom_killed,
            f"Memory usage high: {c.memory:.0f}% of container limit",
            memory_pct=round(c.memory, 1),
        )
        warn(c.cpu >= 85, f"CPU throttling: {c.cpu:.0f}% utilization", cpu_pct=round(c.cpu, 1))
        if c.cache_hit_rate is not None:
            warn(
                c.cache_hit_rate < 0.8,
                f"Cache hit rate degraded: {c.cache_hit_rate:.0%} (evictions elevated)",
                hit_rate=round(c.cache_hit_rate, 3),
            )
        if spec.kind is ServiceKind.DATABASE:
            warn(
                c.query_factor > 1.5,
                f"duration: {c.p95:.0f} ms  statement: SELECT o.*, i.* FROM orders o "
                f"JOIN order_items i ON i.order_id = o.id WHERE o.customer_id = $1",
                duration_ms=round(c.p95, 1),
            )
            if c.db_conn_total and spec.max_connections and c.db_conn_total > spec.max_connections:
                log(
                    "ERROR",
                    f"FATAL: sorry, too many clients already "
                    f"({c.db_conn_total}/{spec.max_connections} connections)",
                    {"connections": c.db_conn_total, "max_connections": spec.max_connections},
                )
        return out

    _EXCEPTIONS = (
        "KeyError: 'shipping_address'",
        "TypeError: 'NoneType' object is not subscriptable",
        "ValueError: invalid literal for int() with base 10: ''",
        "AttributeError: 'Order' object has no attribute 'discount_code'",
    )

    def _error_log(
        self, c: _Computed, cause: str, route: str, now: datetime, log_instance: str | None
    ) -> LogRecord:
        spec = c.spec
        kind, _, key = cause.partition(":")
        trace = self._trace_id()
        attrs: dict[str, object] = {"route": route}
        if kind == "dep":
            dep_kind = self.specs[key].kind
            attrs.update(dependency=key)
            if self._healthy_from_sample(key) == 0:
                msg = f"Call to {key} failed: connection refused (no healthy instances)"
                attrs.update(error="connection_refused")
            elif dep_kind is ServiceKind.DATABASE:
                msg = f"Query on {key} failed: canceling statement due to statement timeout"
                attrs.update(error="statement_timeout")
            elif dep_kind is ServiceKind.CACHE:
                msg = f"Redis command on {key} failed: timeout reading from socket"
                attrs.update(error="cache_timeout")
            else:
                msg = f"Call to {key} failed: upstream returned 503 Service Unavailable"
                attrs.update(error="upstream_503")
        elif key == "pool":
            msg = (
                f"Timeout acquiring DB connection after {POOL_ACQUIRE_TIMEOUT_MS:.0f}ms "
                f"(pool active={c.pool_active}/{c.pool_max}, waiting={c.pool_waiting})"
            )
            attrs.update(pool_active=c.pool_active, pool_max=c.pool_max, waiting=c.pool_waiting)
        elif key == "burst":
            exc = self._EXCEPTIONS[sum(map(ord, spec.name)) % len(self._EXCEPTIONS)]
            msg = f"Unhandled exception in {route}: {exc}"
            attrs.update(exception=exc.split(":")[0])
        elif key == "shed":
            msg = (
                "Request rejected with 503: instance overloaded, shedding load "
                f"(offered {c.offered_rps:.0f} rps)"
            )
            attrs.update(error="load_shed")
        elif key == "too_many_connections":
            msg = "Connection rejected: too many clients already"
            attrs.update(error="too_many_connections")
        elif spec.kind is ServiceKind.DATABASE:
            msg = "ERROR: could not serialize access due to concurrent update"
            attrs.update(error="serialization_failure")
        elif spec.kind is ServiceKind.CACHE:
            msg = "Client connection closed unexpectedly (read error)"
            attrs.update(error="client_disconnect")
        else:
            msg = f"{route} 500 Internal Server Error: transient failure (connection reset by peer)"
            attrs.update(error="transient")
        return LogRecord(spec.name, now, "ERROR", msg, log_instance, trace, attrs)

    def _healthy_from_sample(self, service: str) -> int:
        return self._healthy(service, self._faults_by_service())

    # ---- convenience for backfill / tests -------------------------------------------

    def run(self, start: datetime, ticks: int) -> list[TickResult]:
        step = timedelta(seconds=self.tick_seconds)
        return [self.tick(start + i * step) for i in range(ticks)]

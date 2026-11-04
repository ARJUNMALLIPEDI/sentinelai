"""Worker jobs: run the simulation, persist telemetry, apply injected faults.

Run with:  arq app.worker.settings.WorkerSettings

Concurrency model
-----------------
arq runs jobs as asyncio tasks in one event loop. ``tick`` and
``inject_fault`` can therefore interleave, but only at ``await`` points, and
every engine call (``tick``, ``inject_fault``, ``clear_fault``) is synchronous.
So engine state is never observed half-updated, with no locks needed.

The engine lives in this process's memory. That means exactly **one**
simulation worker: a second replica would run a second, divergent fleet.
Engine state (active faults, leaked connections, versions) is lost on restart;
the fleet restarts healthy and the backfill fills the gap in history.
"""

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import build_engine, build_sessionmaker
from app.domain.enums import DeploymentKind, LogLevel
from app.logs.ingest import ingest_logs
from app.monitoring.snapshots import FAULTS_KEY, HEARTBEAT_KEY, write_snapshots
from app.repositories import catalog
from app.repositories import telemetry as repo
from app.schemas.telemetry import LogIn, MetricSnapshot
from app.simulation.engine import SimulationEngine, TickResult
from app.simulation.faults import FaultType, InvalidFaultError
from app.simulation.topology import FLEET

logger = logging.getLogger("sentinel.worker")

# ---- Prometheus: the simulated fleet (bounded labels: one per service) -------------
_FLEET_GAUGES = {
    field_: Gauge(f"sentinel_sim_{name}", help_, ["service"])
    for field_, name, help_ in (
        ("cpu_pct", "cpu_percent", "Simulated CPU utilisation"),
        ("memory_pct", "memory_percent", "Simulated memory utilisation"),
        ("rps", "requests_per_second", "Simulated request rate"),
        ("latency_p95_ms", "latency_p95_milliseconds", "Simulated p95 latency"),
        ("error_rate", "error_ratio", "Simulated share of failed requests"),
        ("instances_healthy", "instances_healthy", "Healthy instances"),
    )
}
SIM_DB_CONNECTIONS = Gauge(
    "sentinel_sim_db_connections_active", "DB connections in use", ["service"]
)
SIM_ACTIVE_FAULTS = Gauge("sentinel_sim_active_faults", "Faults currently injected")
SIM_TICKS = Counter("sentinel_sim_ticks_total", "Simulation ticks processed")
SIM_TICK_DURATION = Histogram(
    "sentinel_sim_tick_duration_seconds",
    "Time to run and persist one tick",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)

BACKFILL_CHUNK_TICKS = 60


@dataclass
class WorkerState:
    settings: Settings
    engine: SimulationEngine
    db_engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    service_ids: dict[str, Any] = field(default_factory=dict)
    last_tick: datetime | None = None


def align(now: datetime, tick_seconds: int) -> datetime:
    """Floor to the tick grid so every service's sample shares one timestamp."""
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % tick_seconds, tz=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---- lifecycle ---------------------------------------------------------------------


async def build_state(settings: Settings, now: datetime | None = None) -> WorkerState:
    db_engine = build_engine(settings)
    state = WorkerState(
        settings=settings,
        engine=SimulationEngine(
            FLEET, seed=settings.sim_seed, tick_seconds=settings.sim_tick_seconds
        ),
        db_engine=db_engine,
        sessionmaker=build_sessionmaker(db_engine),
        redis=Redis.from_url(settings.redis_url, decode_responses=True),
    )
    versions = {s.name: state.engine.version_of(s.name) for s in FLEET}
    async with state.sessionmaker() as session:
        state.service_ids = await catalog.sync_topology(session, FLEET, versions)
        await session.commit()
    await publish_faults(state)  # a restarted engine has no faults; clear stale state
    await backfill(state, now or utcnow())
    return state


async def close_state(state: WorkerState) -> None:
    await state.redis.aclose()
    await state.db_engine.dispose()


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    start_http_server(settings.worker_metrics_port)
    ctx["state"] = await build_state(settings)
    logger.info(
        "worker_started",
        extra={"ctx": {"tick_seconds": settings.sim_tick_seconds, "seed": settings.sim_seed}},
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    if state := ctx.get("state"):
        await close_state(state)


# ---- persistence ---------------------------------------------------------------------


async def persist(state: WorkerState, results: Sequence[TickResult], now: datetime) -> None:
    ids = state.service_ids
    samples: list[dict[str, Any]] = []
    logs: list[LogIn] = []
    async with state.sessionmaker() as session:
        for result in results:
            for s in result.samples:
                row = asdict(s)
                row["service_id"] = ids[row.pop("service")]
                samples.append(row)
            logs.extend(
                LogIn(
                    service=r.service,
                    ts=r.ts,
                    level=LogLevel(r.level),
                    message=r.message,
                    instance=r.instance,
                    trace_id=r.trace_id,
                    attributes=dict(r.attributes),
                )
                for r in result.logs
            )
            for d in result.deployments:
                await repo.insert_deployment(
                    session,
                    service_id=ids[d.service],
                    version=d.version,
                    previous_version=d.previous_version,
                    kind=DeploymentKind(d.kind),
                    initiated_by=d.initiated_by,
                    deployed_at=d.ts,
                )
        await repo.insert_samples(session, samples)
        ingested = await ingest_logs(session, logs, ids, now)
        await session.commit()
    if ingested.rejected:
        logger.warning("logs_rejected", extra={"ctx": {"count": len(ingested.rejected)}})


async def backfill(state: WorkerState, now: datetime) -> int:
    """Fill missing history up to now so charts and detectors have data at once.

    Only the gap since the last stored sample is generated (bounded by
    SIM_BACKFILL_MINUTES), so a quick restart doesn't rewrite history.
    """
    tick = timedelta(seconds=state.settings.sim_tick_seconds)
    end = align(now, state.settings.sim_tick_seconds) - tick
    start = end - timedelta(minutes=state.settings.sim_backfill_minutes)
    async with state.sessionmaker() as session:
        latest = await repo.latest_sample_ts(session)
    if latest is not None:
        start = max(start, latest + tick)
    if start > end or state.settings.sim_backfill_minutes == 0:
        return 0

    total = int((end - start) / tick) + 1
    t = start
    while t <= end:
        chunk = []
        for _ in range(BACKFILL_CHUNK_TICKS):
            if t > end:
                break
            chunk.append(state.engine.tick(t))
            t += tick
        await persist(state, chunk, now)
    state.last_tick = end
    logger.info("backfill_complete", extra={"ctx": {"ticks": total, "from": start.isoformat()}})
    return total


async def publish_faults(state: WorkerState) -> None:
    faults = [f.as_dict() for f in state.engine.active_faults]
    await state.redis.set(FAULTS_KEY, json.dumps(faults), ex=state.settings.snapshot_ttl_s)
    SIM_ACTIVE_FAULTS.set(len(faults))


# ---- jobs ------------------------------------------------------------------------------


async def run_tick(state: WorkerState, now: datetime) -> TickResult | None:
    ts = align(now, state.settings.sim_tick_seconds)
    if state.last_tick is not None and ts <= state.last_tick:
        return None  # cron fired twice in one interval; never tick a timestamp twice
    started = time.perf_counter()
    result = state.engine.tick(ts)
    state.last_tick = ts
    await persist(state, [result], now)

    ttl = state.settings.snapshot_ttl_s
    await write_snapshots(state.redis, (MetricSnapshot(**asdict(s)) for s in result.samples), ttl)
    await state.redis.set(HEARTBEAT_KEY, ts.isoformat(), ex=ttl)
    await publish_faults(state)

    for s in result.samples:
        for attr, gauge in _FLEET_GAUGES.items():
            gauge.labels(s.service).set(getattr(s, attr))
        if s.db_conn_active is not None:
            SIM_DB_CONNECTIONS.labels(s.service).set(s.db_conn_active)
    SIM_TICKS.inc()
    SIM_TICK_DURATION.observe(time.perf_counter() - started)
    return result


async def tick(ctx: dict[str, Any]) -> None:
    await run_tick(ctx["state"], utcnow())


async def inject_fault(
    ctx: dict[str, Any],
    fault_id: str,
    fault_type: str,
    service: str,
    intensity: float,
    duration_s: int | None,
) -> dict[str, Any]:
    state: WorkerState = ctx["state"]
    try:
        fault = state.engine.inject_fault(
            FaultType(fault_type), service, utcnow(), intensity, duration_s, fault_id
        )
    except InvalidFaultError as exc:
        logger.warning("fault_rejected", extra={"ctx": {"fault_id": fault_id, "reason": str(exc)}})
        return {"ok": False, "error": str(exc)}
    await publish_faults(state)
    logger.info("fault_injected", extra={"ctx": fault.as_dict()})
    return {"ok": True, "fault": fault.as_dict()}


async def clear_fault(
    ctx: dict[str, Any], fault_id: str, actor: str = "simulation"
) -> dict[str, Any]:
    state: WorkerState = ctx["state"]
    try:
        fault = state.engine.clear_fault(fault_id, actor)
    except KeyError:
        return {"ok": False, "error": "fault not active"}
    await publish_faults(state)
    logger.info("fault_cleared", extra={"ctx": {"fault_id": fault_id, "actor": actor}})
    return {"ok": True, "fault": fault.as_dict()}


async def prune(ctx: dict[str, Any]) -> dict[str, int]:
    state: WorkerState = ctx["state"]
    cutoff = utcnow() - timedelta(hours=state.settings.telemetry_retention_hours)
    async with state.sessionmaker() as session:
        counts = await repo.prune_before(session, cutoff)
        await session.commit()
    logger.info("telemetry_pruned", extra={"ctx": counts})
    return counts

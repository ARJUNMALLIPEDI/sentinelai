"""Worker pipeline against real Postgres and Redis: catalog seeding, backfill,
ticking, snapshots, fault jobs and retention."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.models import Deployment, LogEntry, MetricSample, Service, ServiceDependency
from app.monitoring.snapshots import FAULTS_KEY, HEARTBEAT_KEY, SNAPSHOT_KEY
from app.repositories import catalog
from app.repositories import telemetry as repo
from app.simulation.topology import FLEET
from app.worker import jobs
from app.worker.jobs import WorkerState, build_state, close_state, run_tick, utcnow

pytestmark = pytest.mark.integration

N_SERVICES = len(FLEET)
N_EDGES = sum(len(s.dependencies) for s in FLEET)


async def count(state: WorkerState, model: type, *where: object) -> int:
    async with state.sessionmaker() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def test_startup_seeds_catalog_and_backfills_history(worker: WorkerState) -> None:
    assert await count(worker, Service) == N_SERVICES
    assert await count(worker, ServiceDependency) == N_EDGES
    ticks = worker.settings.sim_backfill_minutes * 60 // worker.settings.sim_tick_seconds
    samples = await count(worker, MetricSample)
    # One sample per service per tick; +1 tick because both window ends are inclusive.
    assert samples in (ticks * N_SERVICES, (ticks + 1) * N_SERVICES)
    assert await count(worker, LogEntry) > 0

    async with worker.sessionmaker() as s:
        orders = await catalog.get_service(s, "orders-service")
        assert orders is not None
        assert orders.slo_latency_p95_ms == 400
        assert orders.current_version == worker.engine.version_of("orders-service")
        assert {d.depends_on.name for d in orders.dependencies} == {
            "postgres-primary",
            "payments-service",
            "inventory-service",
            "notification-service",
        }


async def test_restart_keeps_ids_and_only_fills_the_gap(
    worker: WorkerState, settings: Settings
) -> None:
    before_samples = await count(worker, MetricSample)
    async with worker.sessionmaker() as s:
        ids_before = await catalog.service_ids(s)

    # Restart 30 seconds later: catalog re-sync must be idempotent and the
    # backfill must only cover the missing ticks, never duplicate history.
    restarted = await build_state(settings, now=utcnow() + timedelta(seconds=30))
    try:
        async with restarted.sessionmaker() as s:
            assert await catalog.service_ids(s) == ids_before
        assert await count(restarted, ServiceDependency) == N_EDGES
        gained = await count(restarted, MetricSample) - before_samples
        assert 0 < gained <= 8 * N_SERVICES
        async with restarted.sessionmaker() as s:
            dupes = await s.execute(
                select(MetricSample.service_id, MetricSample.ts)
                .group_by(MetricSample.service_id, MetricSample.ts)
                .having(func.count() > 1)
            )
            assert dupes.all() == []
    finally:
        await close_state(restarted)


async def test_tick_persists_and_publishes_snapshots(worker: WorkerState) -> None:
    before = await count(worker, MetricSample)
    result = await run_tick(worker, utcnow())
    assert result is not None
    assert await count(worker, MetricSample) == before + N_SERVICES

    raw = await worker.redis.get(SNAPSHOT_KEY.format(service="orders-service"))
    assert raw is not None
    snap = json.loads(raw)
    sample = next(s for s in result.samples if s.service == "orders-service")
    assert snap["latency_p95_ms"] == sample.latency_p95_ms
    ttl = await worker.redis.ttl(SNAPSHOT_KEY.format(service="orders-service"))
    assert 0 < ttl <= worker.settings.snapshot_ttl_s
    assert await worker.redis.get(HEARTBEAT_KEY) == result.ts.isoformat()


async def test_the_same_tick_is_never_processed_twice(worker: WorkerState) -> None:
    now = utcnow()
    assert await run_tick(worker, now) is not None
    before = await count(worker, LogEntry)
    assert await run_tick(worker, now) is None  # cron double-fire in one interval
    assert await count(worker, LogEntry) == before


async def test_fault_jobs_change_the_fleet_and_are_published(worker: WorkerState) -> None:
    ctx = {"state": worker}
    out = await jobs.inject_fault(
        ctx, "flt_test", "db_connection_leak", "orders-service", 1.0, None
    )
    assert out["ok"] is True
    published = json.loads(await worker.redis.get(FAULTS_KEY) or "[]")
    assert [f["id"] for f in published] == ["flt_test"]

    t = utcnow()
    for i in range(1, 31):
        await run_tick(worker, t + timedelta(seconds=5 * i))
    async with worker.sessionmaker() as s:
        orders = await catalog.get_service(s, "orders-service")
        assert orders is not None
        latest = (await repo.metric_series(s, orders.id, t, t + timedelta(minutes=5), 1))[-1]
    assert latest.db_conn_active == latest.db_conn_max
    timeouts = await count(
        worker, LogEntry, LogEntry.message.like("Timeout acquiring DB connection%")
    )
    assert timeouts > 0

    assert (await jobs.clear_fault(ctx, "flt_test", "tester"))["ok"] is True
    assert json.loads(await worker.redis.get(FAULTS_KEY) or "[]") == []
    assert (await jobs.clear_fault(ctx, "flt_test"))["ok"] is False


async def test_invalid_fault_job_is_rejected_not_raised(worker: WorkerState) -> None:
    out = await jobs.inject_fault(
        {"state": worker}, "flt_bad", "slow_queries", "orders-service", 0.5, None
    )
    assert out == {"ok": False, "error": "slow_queries cannot be applied to orders-service (api)"}


async def test_bad_deploy_is_recorded_and_updates_current_version(worker: WorkerState) -> None:
    before = worker.engine.version_of("payments-service")
    await jobs.inject_fault(
        {"state": worker}, "flt_dep", "latency_regression", "payments-service", 0.8, None
    )
    await run_tick(worker, utcnow())
    async with worker.sessionmaker() as s:
        deploys = (await s.execute(select(Deployment))).scalars().all()
        svc = await catalog.get_service(s, "payments-service")
    bad = [d for d in deploys if d.previous_version == before]
    assert len(bad) == 1 and bad[0].initiated_by == "ci-pipeline"
    assert svc is not None and svc.current_version == bad[0].version != before


async def test_prune_removes_only_telemetry_older_than_retention(worker: WorkerState) -> None:
    total = await count(worker, MetricSample)
    async with worker.sessionmaker() as s:
        cutoff = datetime.now(UTC) - timedelta(minutes=5)
        counts = await repo.prune_before(s, cutoff)
        await s.commit()
    remaining = await count(worker, MetricSample)
    assert counts["metric_samples"] > 0
    assert remaining == total - counts["metric_samples"]
    assert await count(worker, MetricSample, MetricSample.ts < cutoff) == 0

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings, get_db, get_redis
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.models import Service
from app.monitoring.health import HealthPolicy, HealthStatus, evaluate_health
from app.monitoring.snapshots import read_snapshots
from app.repositories import catalog
from app.repositories import telemetry as repo
from app.schemas.fleet import (
    DependencyOut,
    FleetOverview,
    HealthOut,
    ServiceDetail,
    ServiceSummary,
)
from app.schemas.telemetry import MetricPoint, MetricSeries, MetricSnapshot

router = APIRouter(prefix="/services", tags=["services"])

Db = Annotated[AsyncSession, Depends(get_db)]
Cache = Annotated[Redis, Depends(get_redis)]
Config = Annotated[Settings, Depends(get_app_settings)]


def _summary(
    svc: Service, snap: MetricSnapshot | None, settings: Settings, now: datetime
) -> ServiceSummary:
    policy = HealthPolicy(
        slo_latency_p95_ms=svc.slo_latency_p95_ms, slo_error_rate=svc.slo_error_rate
    )
    health = evaluate_health(snap, policy, now, timedelta(seconds=settings.snapshot_ttl_s))
    return ServiceSummary(
        name=svc.name,
        kind=svc.kind,
        tier=svc.tier,
        description=svc.description,
        desired_instances=svc.desired_instances,
        current_version=svc.current_version,
        slo_latency_p95_ms=svc.slo_latency_p95_ms,
        slo_error_rate=svc.slo_error_rate,
        health=HealthOut.from_assessment(health),
        latest=snap,
    )


async def _require(db: AsyncSession, name: str) -> Service:
    svc = await catalog.get_service(db, name)
    if svc is None:
        raise NotFoundError(f"service '{name}' not found")
    return svc


@router.get("", response_model=FleetOverview)
async def list_services(db: Db, redis: Cache, settings: Config) -> FleetOverview:
    services = await catalog.list_services(db)
    snaps = await read_snapshots(redis, [s.name for s in services])
    now = datetime.now(UTC)
    summaries = [_summary(s, snaps[s.name], settings, now) for s in services]
    counts = Counter(s.health.status for s in summaries)
    return FleetOverview(
        generated_at=now,
        counts={status: counts.get(status, 0) for status in HealthStatus},
        services=summaries,
    )


@router.get("/{name}", response_model=ServiceDetail)
async def get_service(name: str, db: Db, redis: Cache, settings: Config) -> ServiceDetail:
    svc = await _require(db, name)
    snap = (await read_snapshots(redis, [name]))[name]
    summary = _summary(svc, snap, settings, datetime.now(UTC))
    return ServiceDetail(
        **summary.model_dump(),
        dependencies=[
            DependencyOut(
                service=d.depends_on.name,
                calls_per_request=d.calls_per_request,
                criticality=d.criticality,
                is_async=d.is_async,
            )
            for d in sorted(svc.dependencies, key=lambda d: d.depends_on.name)
        ],
        dependents=await catalog.dependents_of(db, svc.id),
    )


@router.get("/{name}/health", response_model=HealthOut)
async def get_health(name: str, db: Db, redis: Cache, settings: Config) -> HealthOut:
    svc = await _require(db, name)
    snap = (await read_snapshots(redis, [name]))[name]
    return _summary(svc, snap, settings, datetime.now(UTC)).health


@router.get("/{name}/metrics", response_model=MetricSeries)
async def get_metrics(
    name: str,
    db: Db,
    settings: Config,
    minutes: Annotated[int, Query(ge=1, le=180, description="Window length")] = 60,
    until: Annotated[datetime | None, Query(description="Window end (default: now)")] = None,
) -> MetricSeries:
    svc = await _require(db, name)
    end = until or datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    start = end - timedelta(minutes=minutes)
    max_points = minutes * 60 // settings.sim_tick_seconds + 1
    rows = await repo.metric_series(db, svc.id, start, end, max_points)
    return MetricSeries(
        service=name,
        start=start,
        end=end,
        points=[MetricPoint.model_validate(r) for r in rows],
    )

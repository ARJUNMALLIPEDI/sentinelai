"""Liveness vs readiness.

* ``/health/live``  — "is the process up?" Never touches dependencies, so an
  orchestrator won't restart the API just because Postgres blipped.
* ``/health/ready`` — "can I serve traffic?" Checks Postgres and Redis
  concurrently, each with a timeout, and returns 503 if any fail.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import __version__
from app.api.deps import get_app_settings, get_engine, get_redis
from app.core.config import Settings
from app.schemas.health import DependencyCheck, LivenessResponse, ReadinessResponse

router = APIRouter(prefix="/health", tags=["health"])
logger = logging.getLogger(__name__)


async def _timed_check(name: str, check: Awaitable[Any], timeout_s: float) -> DependencyCheck:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(check, timeout=timeout_s)
        status_, error = "ok", None
    except Exception as exc:  # any failure means "not ready"; record which kind
        status_, error = "error", type(exc).__name__
        logger.warning(
            "dependency_check_failed", extra={"ctx": {"dependency": name, "error": error}}
        )
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    return DependencyCheck(status=status_, latency_ms=latency_ms, error=error)


async def _check_postgres(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check_redis(redis: Redis) -> None:
    await redis.ping()


@router.get("/live", response_model=LivenessResponse)
async def live() -> LivenessResponse:
    return LivenessResponse(version=__version__)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def ready(
    response: Response,
    settings: Annotated[Settings, Depends(get_app_settings)],
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ReadinessResponse:
    timeout = settings.health_check_timeout_s
    pg, rd = await asyncio.gather(
        _timed_check("postgres", _check_postgres(engine), timeout),
        _timed_check("redis", _check_redis(redis), timeout),
    )
    checks = {"postgres": pg, "redis": rd}
    healthy = all(c.status == "ok" for c in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ok" if healthy else "degraded", checks=checks)

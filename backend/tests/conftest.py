import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.main import create_app
from app.worker.jobs import WorkerState, build_state, close_state

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel_test"
)
# DB 15 keeps test keys away from the dev instance's DB 0.
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "log_json": False,
        "log_level": "WARNING",
        "health_check_timeout_s": 1.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


BACKEND = Path(__file__).resolve().parents[1]
TELEMETRY_TABLES = (
    "agent_steps",
    "incident_reports",
    "investigations",
    "incident_events",
    "anomalies",
    "incident_services",
    "incidents",
    "log_entries",
    "metric_samples",
    "deployments",
    "service_dependencies",
    "services",
)


def alembic_config() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def migrated_db() -> None:
    """Bring the test database to the latest schema once per session."""
    command.upgrade(alembic_config(), "head")


@pytest.fixture
async def clean_db(migrated_db: None) -> AsyncIterator[None]:
    """Empty tables and the test Redis DB so every test starts from nothing."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TELEMETRY_TABLES)} RESTART IDENTITY CASCADE"))
    await engine.dispose()
    redis = Redis.from_url(TEST_REDIS_URL)
    await redis.flushdb()
    await redis.aclose()
    yield


@pytest.fixture
def settings() -> Settings:
    return make_settings(sim_backfill_minutes=10, sim_seed=7)


@pytest.fixture
async def worker(settings: Settings, clean_db: None) -> AsyncIterator[WorkerState]:
    """A worker that has seeded the catalog and backfilled ``sim_backfill_minutes``."""
    state = await build_state(settings)
    yield state
    await close_state(state)


@pytest.fixture
def client_factory() -> Callable[..., Any]:
    """Build a client for an app, optionally customised before startup."""

    def _factory(settings: Settings, configure: Callable[[FastAPI], None] | None = None) -> Any:
        app = create_app(settings)
        if configure:
            configure(app)

        class _Ctx:
            async def __aenter__(self) -> AsyncClient:
                self._lifespan = app.router.lifespan_context(app)
                await self._lifespan.__aenter__()
                transport = ASGITransport(app=app, raise_app_exceptions=False)
                self._client = AsyncClient(transport=transport, base_url="http://test")
                return await self._client.__aenter__()

            async def __aexit__(self, *exc: object) -> None:
                await self._client.__aexit__(*exc)
                await self._lifespan.__aexit__(*exc)

        return _Ctx()

    return _factory


@pytest.fixture
async def client(
    settings: Settings, client_factory: Callable[..., Any]
) -> AsyncIterator[AsyncClient]:
    async with client_factory(settings) as c:
        yield c

import os
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app

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


@pytest.fixture
def settings() -> Settings:
    return make_settings()


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

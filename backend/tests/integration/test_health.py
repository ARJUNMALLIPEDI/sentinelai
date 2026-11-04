from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.conftest import make_settings

pytestmark = pytest.mark.integration


async def test_liveness_does_not_touch_dependencies(client_factory: Callable[..., Any]) -> None:
    # Point both dependencies at closed ports: liveness must still be 200.
    broken = make_settings(
        database_url="postgresql://x:x@127.0.0.1:1/x", redis_url="redis://127.0.0.1:1/0"
    )
    async with client_factory(broken) as client:
        r = await client.get("/api/v1/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_readiness_ok_when_postgres_and_redis_are_up(client: AsyncClient) -> None:
    r = await client.get("/api/v1/health/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["postgres"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "ok"


async def test_readiness_503_and_names_the_failed_dependency(
    client_factory: Callable[..., Any],
) -> None:
    async with client_factory(make_settings(redis_url="redis://127.0.0.1:1/0")) as client:
        r = await client.get("/api/v1/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "error"
    assert body["checks"]["redis"]["error"]  # exception class name, no connection details


async def test_request_id_is_generated_or_propagated(client: AsyncClient) -> None:
    generated = await client.get("/api/v1/health/live")
    assert len(generated.headers["x-request-id"]) == 32

    propagated = await client.get("/api/v1/health/live", headers={"X-Request-ID": "gw-abc.123"})
    assert propagated.headers["x-request-id"] == "gw-abc.123"

    # Unsafe values (header injection, absurd length) are replaced, not echoed.
    unsafe = await client.get("/api/v1/health/live", headers={"X-Request-ID": "x" * 500})
    assert unsafe.headers["x-request-id"] != "x" * 500


async def test_metrics_use_route_templates_not_raw_paths(client: AsyncClient) -> None:
    await client.get("/api/v1/health/live")
    await client.get("/does-not-exist/12345")
    text = (await client.get("/metrics")).text
    assert 'route="/api/v1/health/live"' in text
    assert 'route="unmatched"' in text
    assert "12345" not in text
    assert 'route="/metrics"' not in text  # scrapes don't count themselves


async def test_metrics_collapse_path_params_into_one_series(
    settings: Any,
    client_factory: Callable[..., Any],
) -> None:
    from fastapi import APIRouter

    def add_param_route(app: FastAPI) -> None:
        router = APIRouter(prefix="/things")

        @router.get("/{thing_id}")
        async def get_thing(thing_id: str) -> dict[str, str]:
            return {"id": thing_id}

        app.include_router(router, prefix="/api/v1")

    async with client_factory(settings, add_param_route) as client:
        for tid in ("a1b2", "c3d4", "e5f6"):
            assert (await client.get(f"/api/v1/things/{tid}")).status_code == 200
        text = (await client.get("/metrics")).text
    assert 'route="/api/v1/things/{thing_id}"' in text
    assert "a1b2" not in text and "e5f6" not in text


async def test_unhandled_exception_returns_json_500_with_request_id(
    settings: Any,
    client_factory: Callable[..., Any],
) -> None:
    def add_failing_route(app: FastAPI) -> None:
        async def boom() -> None:
            raise RuntimeError("database password is hunter2")

        app.add_api_route("/boom", boom)

    async with client_factory(settings, add_failing_route) as client:
        r = await client.get("/boom", headers={"X-Request-ID": "trace-500"})
    assert r.status_code == 500
    assert r.headers["x-request-id"] == "trace-500"
    err = r.json()["error"]
    assert err == {
        "code": "internal_error",
        "message": "An unexpected error occurred.",
        "request_id": "trace-500",
    }
    assert "hunter2" not in r.text


async def test_404_uses_the_standard_error_shape(client: AsyncClient) -> None:
    r = await client.get("/api/v1/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "http_error"
    assert r.json()["error"]["request_id"] == r.headers["x-request-id"]

"""Fleet, logs, deployments and simulation-control APIs over a real worker state."""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from app.simulation.topology import FLEET
from app.worker import jobs
from app.worker.jobs import WorkerState, run_tick, utcnow
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def api(
    worker: WorkerState, settings: Settings, client_factory: Callable[..., Any]
) -> AsyncIterator[AsyncClient]:
    async with client_factory(settings) as c:
        yield c


# --------------------------------------------------------------------------- services


async def test_fleet_without_fresh_snapshots_is_unknown(api: AsyncClient) -> None:
    # Backfill writes history but no snapshots: only a live tick says "now".
    body = (await api.get("/api/v1/services")).json()
    assert len(body["services"]) == len(FLEET)
    assert body["counts"]["unknown"] == len(FLEET)
    assert all(s["latest"] is None for s in body["services"])


async def test_fleet_overview_reports_live_health(api: AsyncClient, worker: WorkerState) -> None:
    await run_tick(worker, utcnow())
    body = (await api.get("/api/v1/services")).json()
    assert body["counts"]["unknown"] == 0 and body["counts"]["down"] == 0
    assert sum(body["counts"].values()) == len(FLEET)
    orders = next(s for s in body["services"] if s["name"] == "orders-service")
    assert orders["latest"]["instances_total"] == 3
    assert orders["slo_latency_p95_ms"] == 400


async def test_outage_shows_as_down_in_health(api: AsyncClient, worker: WorkerState) -> None:
    await jobs.inject_fault(
        {"state": worker}, "flt_o", "service_outage", "inventory-service", 1.0, None
    )
    await run_tick(worker, utcnow())
    health = (await api.get("/api/v1/services/inventory-service/health")).json()
    assert health["status"] == "down"
    assert health["reasons"] == ["0/2 instances healthy"]


async def test_service_detail_exposes_the_dependency_graph(api: AsyncClient) -> None:
    body = (await api.get("/api/v1/services/orders-service")).json()
    deps = {d["service"]: d for d in body["dependencies"]}
    assert set(deps) == {
        "inventory-service",
        "notification-service",
        "payments-service",
        "postgres-primary",
    }
    assert deps["notification-service"]["is_async"] is True
    assert body["dependents"] == ["api-gateway"]


async def test_unknown_service_is_a_404_in_the_standard_error_shape(api: AsyncClient) -> None:
    r = await api.get("/api/v1/services/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


async def test_metric_series_is_ordered_and_windowed(api: AsyncClient, worker: WorkerState) -> None:
    r = await api.get("/api/v1/services/orders-service/metrics", params={"minutes": 5})
    assert r.status_code == 200
    points = r.json()["points"]
    expected = 5 * 60 // worker.settings.sim_tick_seconds
    assert expected - 2 <= len(points) <= expected + 1
    ts = [p["ts"] for p in points]
    assert ts == sorted(ts)

    too_long = await api.get("/api/v1/services/orders-service/metrics", params={"minutes": 999})
    assert too_long.status_code == 422


# --------------------------------------------------------------------------- logs


async def test_logs_filter_by_level_and_service(api: AsyncClient) -> None:
    r = await api.get(
        "/api/v1/logs", params={"level": "INFO", "service": "auth-service", "limit": 50}
    )
    items = r.json()["items"]
    assert items and all(i["level"] == "INFO" and i["service"] == "auth-service" for i in items)


async def test_log_pagination_is_stable_and_newest_first(api: AsyncClient) -> None:
    first = (await api.get("/api/v1/logs", params={"limit": 5})).json()
    assert len(first["items"]) == 5 and first["next_cursor"]
    second = (
        await api.get("/api/v1/logs", params={"limit": 5, "cursor": first["next_cursor"]})
    ).json()
    ids_1 = {i["id"] for i in first["items"]}
    ids_2 = {i["id"] for i in second["items"]}
    assert ids_1.isdisjoint(ids_2)
    keys = [(i["ts"], i["id"]) for i in first["items"] + second["items"]]
    assert keys == sorted(keys, reverse=True)


async def test_log_search_treats_wildcards_literally(api: AsyncClient) -> None:
    # "%" would match everything if passed to LIKE unescaped.
    r = await api.get("/api/v1/logs", params={"q": "%"})
    assert all("%" in i["message"] for i in r.json()["items"])


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"service": "ghost-service"}, "invalid_query"),
        ({"cursor": "not-a-cursor"}, "invalid_query"),
    ],
)
async def test_bad_log_queries_are_422(api: AsyncClient, params: dict[str, str], code: str) -> None:
    r = await api.get("/api/v1/logs", params=params)
    assert r.status_code == 422 and r.json()["error"]["code"] == code


async def test_log_batch_ingestion_is_partial(api: AsyncClient) -> None:
    now = datetime.now(UTC)
    entry = {
        "service": "orders-service",
        "ts": now.isoformat(),
        "level": "ERROR",
        "message": "boom",
    }
    r = await api.post(
        "/api/v1/logs/batch",
        json={
            "entries": [
                entry,
                {**entry, "service": "ghost"},
                {**entry, "ts": (now + timedelta(hours=1)).isoformat()},
                {**entry, "message": "x" * 10_000},
            ]
        },
    )
    assert r.status_code == 202
    body = r.json()
    assert body["accepted"] == 2
    assert {(x["index"], x["reason"]) for x in body["rejected"]} == {
        (1, "unknown service 'ghost'"),
        (2, "timestamp is in the future"),
    }
    stored = (await api.get("/api/v1/logs", params={"q": "xxxx", "limit": 1})).json()["items"][0]
    assert stored["message"].endswith("…[truncated]") and len(stored["message"]) == 8192


async def test_naive_timestamps_are_rejected(api: AsyncClient) -> None:
    r = await api.post(
        "/api/v1/logs/batch",
        json={
            "entries": [
                {
                    "service": "orders-service",
                    "ts": "2026-11-04T10:00:00",
                    "level": "INFO",
                    "message": "m",
                }
            ]
        },
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


# --------------------------------------------------------------------------- deployments


async def test_deployments_endpoint_lists_a_bad_deploy(
    api: AsyncClient, worker: WorkerState
) -> None:
    await jobs.inject_fault(
        {"state": worker}, "flt_d", "latency_regression", "orders-service", 0.8, None
    )
    await run_tick(worker, utcnow())
    r = await api.get("/api/v1/deployments", params={"service": "orders-service"})
    latest = r.json()[0]
    assert latest["kind"] == "deploy" and latest["initiated_by"] == "ci-pipeline"
    assert latest["version"] == worker.engine.version_of("orders-service")


# --------------------------------------------------------------------------- simulation control


async def test_fault_catalog_lists_applicable_services(api: AsyncClient) -> None:
    catalog = {f["type"]: f for f in (await api.get("/api/v1/simulation/fault-types")).json()}
    assert catalog["slow_queries"]["applicable_services"] == ["postgres-primary"]
    assert "notification-service" not in catalog["db_connection_leak"]["applicable_services"]


async def test_fault_injection_is_validated_then_queued(
    api: AsyncClient, worker: WorkerState
) -> None:
    bad = await api.post(
        "/api/v1/simulation/faults", json={"service": "orders-service", "type": "slow_queries"}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_fault"

    ok = await api.post(
        "/api/v1/simulation/faults",
        json={"service": "orders-service", "type": "cpu_spike", "intensity": 0.5},
    )
    assert ok.status_code == 202
    fault_id = ok.json()["fault_id"]
    queued = await worker.redis.zrange("arq:queue", 0, -1)
    assert fault_id in queued  # job id == fault id

    # Simulate the worker picking the job up, then confirm the API sees it.
    await jobs.inject_fault({"state": worker}, fault_id, "cpu_spike", "orders-service", 0.5, None)
    status = (await api.get("/api/v1/simulation/status")).json()
    assert [f["id"] for f in status["active_faults"]] == [fault_id]

    dup = await api.post(
        "/api/v1/simulation/faults", json={"service": "orders-service", "type": "cpu_spike"}
    )
    assert dup.status_code == 409

    assert (await api.delete(f"/api/v1/simulation/faults/{fault_id}")).status_code == 202
    assert (await api.delete("/api/v1/simulation/faults/flt_nope")).status_code == 404


async def test_simulation_status_reflects_the_heartbeat(
    api: AsyncClient, worker: WorkerState
) -> None:
    assert (await api.get("/api/v1/simulation/status")).json()["running"] is False
    await run_tick(worker, utcnow())
    assert (await api.get("/api/v1/simulation/status")).json()["running"] is True


async def test_simulation_control_is_not_mounted_in_production(
    clean_db: None, client_factory: Callable[..., Any]
) -> None:
    async with client_factory(make_settings(environment="production")) as client:
        r = await client.get("/api/v1/simulation/status")
    assert r.status_code == 404

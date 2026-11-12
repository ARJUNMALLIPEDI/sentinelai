"""Detection against real Postgres: the job, the executor, constraints, and the API."""

from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.detection.correlation import CorrelationConfig
from app.detection.evaluate import new_pipeline
from app.domain.enums import AnomalyStatus, IncidentEventKind, IncidentStatus
from app.models import Anomaly, Incident, IncidentEvent, LogEntry, MetricSample, Service
from app.repositories import telemetry as repo
from app.worker import jobs
from app.worker.detection import DETECTION_LOCK_KEY, run_detection
from app.worker.jobs import WorkerState, run_tick, utcnow

pytestmark = pytest.mark.integration

FAST = CorrelationConfig(
    resolve_after=timedelta(seconds=15), auto_resolve_after=timedelta(seconds=30)
)


async def detect(state: WorkerState, config: CorrelationConfig = FAST) -> Any:
    async with state.sessionmaker() as s, s.begin():
        return await run_detection(s, state.settings.sim_tick_seconds, config)


async def advance(state: WorkerState, ticks: int, config: CorrelationConfig = FAST) -> None:
    """Tick the simulation forward, running detection after each tick (like the cron)."""
    start = state.last_tick or utcnow()
    for i in range(1, ticks + 1):
        await run_tick(state, start + timedelta(seconds=state.settings.sim_tick_seconds * i))
        await detect(state, config)


async def scalar(state: WorkerState, stmt: Any) -> Any:
    async with state.sessionmaker() as s:
        return (await s.execute(stmt)).scalar_one()


async def incidents(state: WorkerState) -> list[Incident]:
    async with state.sessionmaker() as s:
        return list((await s.execute(select(Incident).order_by(Incident.number))).scalars())


async def inject(state: WorkerState, fault: str, service: str, intensity: float = 1.0) -> None:
    out = await jobs.inject_fault({"state": state}, f"flt_{fault}", fault, service, intensity, 3600)
    assert out["ok"], out


# --------------------------------------------------------------------------- the job


async def test_healthy_fleet_opens_nothing(worker: WorkerState) -> None:
    await advance(worker, 6)
    assert await incidents(worker) == []
    assert await scalar(worker, select(func.count()).select_from(Anomaly)) == 0


async def test_outage_opens_one_incident_with_evidence(worker: WorkerState) -> None:
    await advance(worker, 2)
    await inject(worker, "service_outage", "inventory-service")
    await advance(worker, 4)

    [inc] = await incidents(worker)
    assert inc.title.startswith("inventory-service is down")
    assert inc.severity.value == "sev1"  # gateway/orders (critical tier) failing hard
    async with worker.sessionmaker() as s:
        kinds = [
            e.kind
            for e in (
                await s.execute(
                    select(IncidentEvent)
                    .where(IncidentEvent.incident_id == inc.id)
                    .order_by(IncidentEvent.id)
                )
            ).scalars()
        ]
        evidence = (
            (await s.execute(select(LogEntry.message).where(LogEntry.incident_id == inc.id)))
            .scalars()
            .all()
        )
    assert kinds[0] is IncidentEventKind.OPENED
    assert IncidentEventKind.ANOMALY_DETECTED in kinds
    assert any("Health check failed: 0/2" in m for m in evidence)


async def test_rerunning_detection_on_the_same_data_changes_nothing(worker: WorkerState) -> None:
    await inject(worker, "service_outage", "inventory-service")
    await advance(worker, 3)
    count = await scalar(worker, select(func.count()).select_from(Anomaly))
    events = await scalar(worker, select(func.count()).select_from(IncidentEvent))
    for _ in range(3):
        await detect(worker)  # no new tick: same window, same findings
    assert await scalar(worker, select(func.count()).select_from(Anomaly)) == count
    assert await scalar(worker, select(func.count()).select_from(IncidentEvent)) == events


async def test_recovery_then_auto_resolve(worker: WorkerState) -> None:
    await inject(worker, "service_outage", "inventory-service")
    await advance(worker, 3)
    await jobs.clear_fault({"state": worker}, "flt_service_outage")
    await advance(worker, 16)  # 80s: anomalies resolve (15s), quiet period (30s) elapses

    [inc] = await incidents(worker)
    assert inc.status is IncidentStatus.RESOLVED and inc.resolved_at is not None
    assert (
        await scalar(
            worker,
            select(func.count()).select_from(Anomaly).where(Anomaly.status == AnomalyStatus.ACTIVE),
        )
        == 0
    )


async def test_acknowledged_incident_is_not_auto_resolved(worker: WorkerState) -> None:
    await inject(worker, "service_outage", "inventory-service")
    await advance(worker, 3)
    async with worker.sessionmaker() as s, s.begin():
        await s.execute(text("UPDATE incidents SET status = 'acknowledged'"))
    await jobs.clear_fault({"state": worker}, "flt_service_outage")
    await advance(worker, 16)
    [inc] = await incidents(worker)
    assert inc.status is IncidentStatus.ACKNOWLEDGED
    assert inc.signals_cleared_at is not None  # recovery is still recorded


async def test_a_concurrent_detection_run_is_skipped(worker: WorkerState) -> None:
    async with worker.sessionmaker() as holder, holder.begin():
        await holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": DETECTION_LOCK_KEY})
        outcome = await detect(worker)
    assert outcome.ran is False


async def test_database_rejects_a_duplicate_active_anomaly(worker: WorkerState) -> None:
    await inject(worker, "service_outage", "inventory-service")
    await advance(worker, 2)
    async with worker.sessionmaker() as s:
        a = (await s.execute(select(Anomaly).limit(1))).scalar_one()
        dup = Anomaly(
            incident_id=a.incident_id,
            service_id=a.service_id,
            signal=a.signal,
            detector=a.detector,
            severity=a.severity,
            first_seen=a.first_seen,
            last_seen=a.last_seen,
            value=1,
            peak_value=1,
            message="dup",
        )
        s.add(dup)
        with pytest.raises(IntegrityError, match="uq_anomalies_active_key"):
            await s.commit()


async def test_postgres_executor_matches_the_in_memory_pipeline(worker: WorkerState) -> None:
    """Same samples through both ``apply`` implementations must give the same outcome."""
    await inject(worker, "db_connection_leak", "orders-service")
    detect_times = []
    start = worker.last_tick or utcnow()
    for i in range(1, 41):
        ts = start + timedelta(seconds=5 * i)
        await run_tick(worker, ts)
        await detect(worker)
        detect_times.append(ts)

    pipe = new_pipeline()
    pipe.config = FAST
    async with worker.sessionmaker() as s:
        names = {sid: n for sid, n in (await s.execute(select(Service.id, Service.name))).all()}
        rows = (await s.execute(select(MetricSample).order_by(MetricSample.ts))).scalars().all()
        db_keys = {
            (names[a.service_id], a.signal.value, a.detector)
            for a in (await s.execute(select(Anomaly))).scalars()
        }
    by_ts: dict[Any, list[Any]] = {}
    for r in rows:
        by_ts.setdefault(r.ts, []).append(
            type(
                "S",
                (),
                {
                    **{
                        c: getattr(r, c)
                        for c in (
                            "ts",
                            "cpu_pct",
                            "memory_pct",
                            "rps",
                            "latency_p95_ms",
                            "error_rate",
                            "db_conn_active",
                            "db_conn_max",
                            "cache_hit_rate",
                            "instances_healthy",
                            "instances_total",
                        )
                    },
                    "service": names[r.service_id],
                },
            )()
        )
    for ts in sorted(by_ts):
        pipe.observe(by_ts[ts])
        if ts in detect_times:
            pipe.step(ts)
    mem_keys = {(s_, sig, d) for rec in pipe.store.incidents.values() for s_, sig, d in rec.signals}

    assert len(pipe.store.incidents) == len(await incidents(worker)) == 1
    assert mem_keys == db_keys
    assert ("orders-service", "db_pool_utilization", "threshold") in db_keys


async def test_retention_keeps_incident_evidence(worker: WorkerState) -> None:
    await inject(worker, "service_outage", "inventory-service")
    await advance(worker, 3)
    linked = await scalar(
        worker, select(func.count()).select_from(LogEntry).where(LogEntry.incident_id.is_not(None))
    )
    assert linked > 0
    async with worker.sessionmaker() as s:
        await repo.prune_before(s, utcnow() + timedelta(days=1))  # "everything is old"
        await s.commit()
    assert await scalar(worker, select(func.count()).select_from(LogEntry)) == linked
    assert await scalar(worker, select(func.count()).select_from(MetricSample)) == 0


# --------------------------------------------------------------------------- API


@pytest.fixture
async def api(
    worker: WorkerState, settings: Settings, client_factory: Callable[..., Any]
) -> AsyncIterator[AsyncClient]:
    async with client_factory(settings) as c:
        yield c


async def test_manual_incident_lifecycle(api: AsyncClient) -> None:
    r = await api.post(
        "/api/v1/incidents",
        json={
            "title": "Checkout errors reported by support",
            "severity": "sev2",
            "services": ["orders-service", "payments-service"],
            "actor": "arjun",
        },
    )
    assert r.status_code == 201
    inc = r.json()
    assert inc["ref"] == "INC-1" and inc["detected_by"] == "manual"
    assert inc["services"] == ["orders-service", "payments-service"]

    for ref in ("INC-1", "1", "inc-1", inc["id"]):
        assert (await api.get(f"/api/v1/incidents/{ref}")).status_code == 200
    assert (await api.get("/api/v1/incidents/INC-999")).status_code == 404
    assert (await api.get("/api/v1/incidents/not-a-ref")).status_code == 404

    ack = (
        await api.patch(
            "/api/v1/incidents/INC-1", json={"status": "investigating", "actor": "arjun"}
        )
    ).json()
    assert ack["status"] == "investigating" and ack["acknowledged_at"] is not None

    back = await api.patch("/api/v1/incidents/INC-1", json={"status": "acknowledged"})
    assert back.status_code == 409 and "allowed" in back.json()["error"]["message"]

    sev = (
        await api.patch(
            "/api/v1/incidents/INC-1", json={"severity": "sev1", "note": "Revenue impact confirmed"}
        )
    ).json()
    assert sev["severity"] == "sev1"
    kinds = [e["kind"] for e in sev["timeline"]]
    assert kinds == ["opened", "status_changed", "severity_changed", "note"]

    note = await api.post("/api/v1/incidents/INC-1/notes", json={"message": "Paged payments team"})
    assert note.status_code == 201 and note.json()["kind"] == "note"

    done = (await api.patch("/api/v1/incidents/INC-1", json={"status": "resolved"})).json()
    assert done["status"] == "resolved" and done["resolved_at"]
    reopened = (await api.patch("/api/v1/incidents/INC-1", json={"status": "open"})).json()
    assert reopened["status"] == "open" and reopened["resolved_at"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"title": "x", "severity": "sev2", "services": ["orders-service"]},
        {"title": "Valid title", "severity": "sev9", "services": ["orders-service"]},
        {"title": "Valid title", "severity": "sev2", "services": []},
        {
            "title": "Valid title",
            "severity": "sev2",
            "services": ["orders-service"],
            "actor": "<script>",
        },
    ],
)
async def test_manual_incident_validation(api: AsyncClient, body: dict[str, Any]) -> None:
    assert (await api.post("/api/v1/incidents", json=body)).status_code == 422


async def test_unknown_service_on_manual_incident(api: AsyncClient) -> None:
    r = await api.post(
        "/api/v1/incidents",
        json={"title": "Valid title", "severity": "sev3", "services": ["ghost"]},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_incident"


async def test_empty_patch_is_rejected(api: AsyncClient) -> None:
    await api.post(
        "/api/v1/incidents",
        json={"title": "Valid title", "severity": "sev3", "services": ["orders-service"]},
    )
    assert (await api.patch("/api/v1/incidents/INC-1", json={"actor": "x"})).status_code == 422


async def test_detected_incident_through_the_api(api: AsyncClient, worker: WorkerState) -> None:
    await inject(worker, "service_outage", "inventory-service")
    await advance(worker, 3)

    [summary] = (await api.get("/api/v1/incidents", params={"active": "true"})).json()
    assert summary["detected_by"] == "detector" and summary["active_anomalies"] > 0
    assert "inventory-service" in summary["services"]

    detail = (await api.get(f"/api/v1/incidents/{summary['ref']}")).json()
    assert {a["service"] for a in detail["anomalies"]} >= {"inventory-service"}
    logs = (await api.get(f"/api/v1/incidents/{summary['ref']}/logs")).json()
    assert logs and all(entry["level"] in {"WARNING", "ERROR", "CRITICAL"} for entry in logs)

    active = (await api.get("/api/v1/anomalies", params={"status": "active"})).json()
    assert len(active) == summary["active_anomalies"]

    # Resolving by hand closes the anomalies with it...
    await api.patch(f"/api/v1/incidents/{summary['ref']}", json={"status": "resolved"})
    assert (await api.get("/api/v1/anomalies", params={"status": "active"})).json() == []
    assert (await api.get("/api/v1/incidents", params={"active": "true"})).json() == []

    # ...so if the problem is still there, detection opens a fresh incident.
    await advance(worker, 2)
    reopened = (await api.get("/api/v1/incidents", params={"active": "true"})).json()
    assert len(reopened) == 1 and reopened[0]["ref"] != summary["ref"]

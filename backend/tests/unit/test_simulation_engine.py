"""The simulator is the ground truth every later milestone is tested against,
so these tests pin down behaviour, not just "it runs": realism at baseline,
how each failure propagates through the dependency graph, and that logs never
contradict metrics."""

import statistics
from datetime import UTC, datetime, timedelta

import pytest

from app.simulation.engine import (
    POOL_ACQUIRE_TIMEOUT_MS,
    RESTART_SECONDS,
    MetricSample,
    SimulationEngine,
    TickResult,
)
from app.simulation.faults import FaultType, InvalidFaultError
from app.simulation.topology import (
    FLEET,
    Dependency,
    ServiceKind,
    ServiceSpec,
    ServiceTier,
    TopologyError,
    dependencies_first,
    index_by_name,
)

PEAK = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
TICK = 5
SLO = {s.name: s for s in FLEET}


def by_service(result: TickResult) -> dict[str, MetricSample]:
    return {s.service: s for s in result.samples}


def warmed(seed: int = 7, ticks: int = 24) -> tuple[SimulationEngine, datetime]:
    """Engine past its noise warm-up, plus the timestamp of the next tick."""
    engine = SimulationEngine(seed=seed, tick_seconds=TICK)
    engine.run(PEAK, ticks)
    return engine, PEAK + timedelta(seconds=TICK * ticks)


def run_with_fault(
    fault: FaultType, service: str, ticks: int = 40, intensity: float = 0.8
) -> list[TickResult]:
    engine, now = warmed()
    engine.inject_fault(fault, service, now, intensity)
    return engine.run(now, ticks)


# --------------------------------------------------------------------------- topology


def test_dependencies_come_before_their_callers() -> None:
    specs = index_by_name(FLEET)
    order = dependencies_first(specs)
    assert sorted(order) == sorted(specs)
    for spec in FLEET:
        for dep in spec.dependencies:
            assert order.index(dep.target) < order.index(spec.name)


def _spec(name: str, *deps: str) -> ServiceSpec:
    return ServiceSpec(
        name=name,
        kind=ServiceKind.API,
        tier=ServiceTier.STANDARD,
        description="",
        instances=1,
        capacity_rps_per_instance=10,
        base_latency_ms=1,
        base_cpu_pct=1,
        base_memory_pct=1,
        base_error_rate=0,
        slo_latency_p95_ms=1,
        slo_error_rate=0.1,
        request_kb=1,
        response_kb=1,
        dependencies=tuple(Dependency(d, 1, 1) for d in deps),
    )


def test_topology_rejects_cycles_and_unknown_targets() -> None:
    with pytest.raises(TopologyError, match="cycle"):
        dependencies_first(index_by_name([_spec("a", "b"), _spec("b", "a")]))
    with pytest.raises(TopologyError, match="unknown"):
        dependencies_first(index_by_name([_spec("a", "ghost")]))
    with pytest.raises(TopologyError, match="duplicate"):
        index_by_name([_spec("a"), _spec("a")])


# --------------------------------------------------------------------------- determinism


def _scripted_run(seed: int) -> list[TickResult]:
    engine, now = warmed(seed=seed, ticks=10)
    engine.inject_fault(FaultType.ERROR_BURST, "orders-service", now, 0.5, fault_id="flt_x")
    return engine.run(now, 30)


def test_same_seed_same_world() -> None:
    assert _scripted_run(123) == _scripted_run(123)


def test_different_seed_different_world() -> None:
    assert _scripted_run(1) != _scripted_run(2)


# --------------------------------------------------------------------------- baseline realism


@pytest.fixture(scope="module")
def baseline_hour() -> list[TickResult]:
    return SimulationEngine(seed=99, tick_seconds=TICK).run(PEAK, 720)


def test_baseline_fleet_meets_its_slos(baseline_hour: list[TickResult]) -> None:
    for name, spec in SLO.items():
        samples = [by_service(r)[name] for r in baseline_hour[20:]]
        ok_latency = sum(s.latency_p95_ms <= spec.slo_latency_p95_ms for s in samples) / len(
            samples
        )
        ok_errors = sum(s.error_rate <= spec.slo_error_rate for s in samples) / len(samples)
        assert ok_latency > 0.97, f"{name} p95 within SLO only {ok_latency:.0%} of the time"
        assert ok_errors > 0.97, f"{name} errors within SLO only {ok_errors:.0%} of the time"
        assert all(0 <= s.cpu_pct <= 100 and 0 <= s.memory_pct <= 100 for s in samples)


def test_baseline_has_no_critical_logs(baseline_hour: list[TickResult]) -> None:
    critical = [log for r in baseline_hour for log in r.logs if log.level == "CRITICAL"]
    assert critical == []


def test_metrics_are_autocorrelated_not_white_noise(baseline_hour: list[TickResult]) -> None:
    cpu = [by_service(r)["orders-service"].cpu_pct for r in baseline_hour[:300]]
    lag1 = statistics.correlation(cpu[:-1], cpu[1:])
    assert lag1 > 0.5, f"lag-1 autocorrelation {lag1:.2f}: series looks like white noise"


def test_traffic_follows_a_daily_cycle() -> None:
    def mean_rps(hour: int) -> float:
        engine = SimulationEngine(seed=5, tick_seconds=TICK)
        start = datetime(2026, 11, 4, hour, 0, tzinfo=UTC)
        return statistics.fmean(by_service(r)["api-gateway"].rps for r in engine.run(start, 60))

    assert mean_rps(18) > 1.6 * mean_rps(6)


# --------------------------------------------------------------------------- propagation


def test_slow_queries_cascade_into_pool_exhaustion_in_heavy_db_users() -> None:
    last = by_service(run_with_fault(FaultType.SLOW_QUERIES, "postgres-primary")[-1])
    for heavy in ("orders-service", "inventory-service"):
        s = last[heavy]
        assert s.db_conn_active == s.db_conn_max, f"{heavy} pool should be exhausted"
        assert s.latency_p95_ms > SLO[heavy].slo_latency_p95_ms
    auth = last["auth-service"]  # light DB user with headroom
    assert auth.db_conn_active is not None and auth.db_conn_max is not None
    assert auth.db_conn_active < auth.db_conn_max
    assert last["api-gateway"].error_rate > SLO["api-gateway"].slo_error_rate


def test_connection_leak_stays_local_to_the_leaking_service() -> None:
    last = by_service(run_with_fault(FaultType.DB_CONNECTION_LEAK, "orders-service")[-1])
    orders = last["orders-service"]
    assert orders.db_conn_active == orders.db_conn_max
    assert orders.error_rate > SLO["orders-service"].slo_error_rate
    for other in ("inventory-service", "payments-service", "auth-service"):
        s = last[other]
        assert s.db_conn_max and s.db_conn_active is not None
        assert s.db_conn_active < 0.8 * s.db_conn_max, f"{other} pool should be unaffected"
    # The database itself is healthy: this is not a DB incident.
    assert last["postgres-primary"].latency_p95_ms <= SLO["postgres-primary"].slo_latency_p95_ms


def test_outage_fails_only_the_share_of_traffic_that_depends_on_it() -> None:
    last = by_service(run_with_fault(FaultType.SERVICE_OUTAGE, "inventory-service", ticks=10)[-1])
    assert last["inventory-service"].instances_healthy == 0
    assert last["inventory-service"].error_rate == 1.0
    assert last["orders-service"].error_rate > 0.8  # hard dependency
    assert 0.3 < last["api-gateway"].error_rate < 0.7  # only some routes touch inventory
    assert last["auth-service"].error_rate < SLO["auth-service"].slo_error_rate


def test_async_dependency_failure_does_not_fail_callers() -> None:
    last = by_service(
        run_with_fault(FaultType.SERVICE_OUTAGE, "notification-service", ticks=10)[-1]
    )
    assert last["notification-service"].instances_healthy == 0
    assert last["orders-service"].error_rate < SLO["orders-service"].slo_error_rate


def test_callers_going_down_removes_downstream_traffic() -> None:
    engine, now = warmed()
    before = by_service(engine.tick(now))["payments-service"].rps
    engine.inject_fault(FaultType.SERVICE_OUTAGE, "orders-service", now, 1.0)
    after = by_service(engine.run(now + timedelta(seconds=TICK), 3)[-1])["payments-service"].rps
    assert before > 20 and after == 0  # payments is only called by orders


def test_cache_degradation_shifts_load_onto_the_database() -> None:
    engine, now = warmed()
    before = by_service(engine.tick(now))
    engine.inject_fault(FaultType.CACHE_DEGRADATION, "redis-cache", now, 1.0)
    after = by_service(engine.run(now + timedelta(seconds=TICK), 20)[-1])
    assert after["redis-cache"].cache_hit_rate is not None
    assert after["redis-cache"].cache_hit_rate < 0.4
    assert after["postgres-primary"].rps > 1.5 * before["postgres-primary"].rps
    assert after["inventory-service"].latency_p95_ms > before["inventory-service"].latency_p95_ms


# --------------------------------------------------------------------------- fault lifecycle


def test_memory_leak_ends_in_oom_kill_and_restart() -> None:
    results = run_with_fault(FaultType.MEMORY_LEAK, "payments-service", ticks=150, intensity=1.0)
    oom = [
        (i, log)
        for i, r in enumerate(results)
        for log in r.logs
        if log.service == "payments-service"
        and log.level == "CRITICAL"
        and "OOMKilled" in log.message
    ]
    assert oom, "expected at least one OOM kill"
    i, log = oom[0]
    assert by_service(results[i])["payments-service"].memory_pct > 90
    assert by_service(results[i + 1])["payments-service"].instances_healthy == 1
    restart_ticks = RESTART_SECONDS // TICK
    recovered = by_service(results[i + restart_ticks + 1])["payments-service"]
    assert recovered.instances_healthy == 2
    assert any(
        f"Instance {log.instance} started" in x.message
        for r in results[i + 1 : i + restart_ticks + 2]
        for x in r.logs
    )


def test_bad_deploy_is_indistinguishable_in_the_deploy_log_and_rolls_back_on_clear() -> None:
    engine, now = warmed()
    baseline_p95 = by_service(engine.tick(now))["orders-service"].latency_p95_ms
    old_version = engine.version_of("orders-service")

    fault = engine.inject_fault(FaultType.LATENCY_REGRESSION, "orders-service", now)
    after = engine.run(now + timedelta(seconds=TICK), 12)
    deploys = [d for r in after for d in r.deployments if d.service == "orders-service"]
    assert len(deploys) == 1
    assert deploys[0].kind == "deploy" and deploys[0].initiated_by == "ci-pipeline"
    assert deploys[0].previous_version == old_version
    assert by_service(after[-1])["orders-service"].latency_p95_ms > 2 * baseline_p95

    engine.clear_fault(fault.id, actor="arjun")
    recovered = engine.run(now + timedelta(seconds=TICK * 14), 12)
    rollbacks = [d for r in recovered for d in r.deployments if d.service == "orders-service"]
    assert rollbacks[0].kind == "rollback" and rollbacks[0].initiated_by == "arjun"
    assert rollbacks[0].version == old_version
    assert engine.version_of("orders-service") == old_version
    assert by_service(recovered[-1])["orders-service"].latency_p95_ms < 1.5 * baseline_p95


def test_faults_expire_after_their_duration() -> None:
    engine, now = warmed()
    engine.inject_fault(FaultType.ERROR_BURST, "auth-service", now, 1.0, duration_s=60)
    during = by_service(engine.run(now, 5)[-1])["auth-service"]
    assert during.error_rate > 0.1 and len(engine.active_faults) == 1
    after = by_service(engine.run(now + timedelta(seconds=90), 5)[-1])["auth-service"]
    assert engine.active_faults == []
    assert after.error_rate < SLO["auth-service"].slo_error_rate


@pytest.mark.parametrize(
    ("fault", "service", "intensity", "match"),
    [
        (FaultType.SLOW_QUERIES, "orders-service", 0.5, "cannot be applied"),
        (FaultType.CACHE_DEGRADATION, "auth-service", 0.5, "cannot be applied"),
        (FaultType.DB_CONNECTION_LEAK, "notification-service", 0.5, "cannot be applied"),
        (FaultType.ERROR_BURST, "orders-service", 1.5, "intensity"),
        (FaultType.ERROR_BURST, "no-such-service", 0.5, "unknown service"),
    ],
)
def test_invalid_faults_are_rejected(
    fault: FaultType, service: str, intensity: float, match: str
) -> None:
    engine = SimulationEngine(seed=1)
    with pytest.raises(InvalidFaultError, match=match):
        engine.inject_fault(fault, service, PEAK, intensity)


def test_duplicate_fault_is_rejected() -> None:
    engine = SimulationEngine(seed=1)
    engine.inject_fault(FaultType.CPU_SPIKE, "orders-service", PEAK)
    with pytest.raises(InvalidFaultError, match="already active"):
        engine.inject_fault(FaultType.CPU_SPIKE, "orders-service", PEAK)


# --------------------------------------------------------------------------- logs


def test_pool_timeout_logs_match_the_metrics_of_the_same_tick() -> None:
    results = run_with_fault(FaultType.DB_CONNECTION_LEAK, "orders-service")
    checked = 0
    for r in results:
        sample = by_service(r)["orders-service"]
        for log in r.logs:
            if log.service == "orders-service" and log.message.startswith("Timeout acquiring"):
                assert log.level == "ERROR"
                assert log.attributes["pool_active"] == sample.db_conn_active
                assert log.attributes["pool_max"] == sample.db_conn_max
                assert f"after {POOL_ACQUIRE_TIMEOUT_MS:.0f}ms" in log.message
                checked += 1
    assert checked > 5


def test_dependency_errors_name_the_failing_dependency() -> None:
    results = run_with_fault(FaultType.SERVICE_OUTAGE, "inventory-service", ticks=20)
    refused = [
        log
        for r in results
        for log in r.logs
        if log.service == "orders-service" and log.attributes.get("error") == "connection_refused"
    ]
    assert refused and all(log.attributes["dependency"] == "inventory-service" for log in refused)


def test_every_log_is_well_formed(baseline_hour: list[TickResult]) -> None:
    for r in baseline_hour[:100]:
        for log in r.logs:
            assert log.level in {"INFO", "WARNING", "ERROR", "CRITICAL"}
            assert log.service in SLO
            assert log.ts == r.ts
            assert log.message

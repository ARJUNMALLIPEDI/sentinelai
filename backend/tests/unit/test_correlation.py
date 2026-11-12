from datetime import UTC, datetime, timedelta

import pytest

from app.detection.base import Finding
from app.detection.correlation import (
    ActiveAnomaly,
    OpenIncident,
    incident_severity,
    incident_title,
    neighbors_from_edges,
    plan,
)
from app.detection.signals import Signal
from app.domain.enums import (
    AnomalySeverity,
    IncidentEventKind,
    IncidentSeverity,
    IncidentStatus,
    ServiceTier,
)
from app.domain.incidents import InvalidTransitionError, check_transition
from app.simulation.topology import FLEET

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=UTC)
NEIGHBORS = neighbors_from_edges((s.name, d.target) for s in FLEET for d in s.dependencies)
TIERS = {s.name: s.tier for s in FLEET}
MAJOR, MINOR, CRIT = AnomalySeverity.MAJOR, AnomalySeverity.MINOR, AnomalySeverity.CRITICAL


def finding(
    service: str,
    signal: Signal = Signal.LATENCY_P95,
    severity: AnomalySeverity = MAJOR,
    detector: str = "threshold",
    value: float = 900.0,
) -> Finding:
    return Finding(service, signal, detector, severity, NOW, value, f"{service} {signal} bad")


def active(
    id_: str,
    service: str,
    incident: str,
    signal: Signal = Signal.LATENCY_P95,
    severity: AnomalySeverity = MAJOR,
    seen_ago: int = 0,
    peak: float = 900.0,
) -> ActiveAnomaly:
    return ActiveAnomaly(
        id_,
        (service, signal, "threshold"),
        incident,
        severity,
        NOW - timedelta(seconds=seen_ago),
        peak,
    )


def incident(
    id_: str,
    services: set[str],
    status: IncidentStatus = IncidentStatus.OPEN,
    severity: IncidentSeverity = IncidentSeverity.SEV2,
    idle: int = 0,
    cleared_ago: int | None = None,
) -> OpenIncident:
    return OpenIncident(
        id_,
        status,
        severity,
        set(services),
        NOW - timedelta(minutes=30),
        NOW - timedelta(seconds=idle),
        NOW - timedelta(seconds=cleared_ago) if cleared_ago is not None else None,
    )


def run(findings, active_=(), incidents=()):  # type: ignore[no-untyped-def]
    return plan(list(findings), list(active_), list(incidents), NEIGHBORS, TIERS, NOW)


def kinds(p, inc: str) -> list[IncidentEventKind]:  # type: ignore[no-untyped-def]
    return [e.kind for e in p.changes[inc].events] if inc in p.changes else []


# --------------------------------------------------------------------------- dedup & grouping


def test_first_finding_opens_an_incident() -> None:
    p = run([finding("orders-service")])
    [inc] = p.new_incidents
    assert inc.severity is IncidentSeverity.SEV2  # major on a critical-tier service
    assert inc.title == "P95 latency breach on orders-service"
    assert [a.incident for a in p.new_anomalies] == [inc.ref]
    assert kinds(p, inc.ref) == [IncidentEventKind.OPENED, IncidentEventKind.ANOMALY_DETECTED]


def test_still_firing_anomaly_is_refreshed_not_duplicated() -> None:
    p = run(
        [finding("orders-service", value=1200)],
        [active("a1", "orders-service", "i1", peak=900)],
        [incident("i1", {"orders-service"})],
    )
    assert p.new_incidents == [] and p.new_anomalies == []
    [t] = p.touched
    assert t.anomaly_id == "a1" and t.peak == 1200


def test_a_cascade_in_one_cycle_becomes_one_incident() -> None:
    cascade = [
        finding("postgres-primary"),
        finding("orders-service"),
        finding("inventory-service"),
        finding("api-gateway", Signal.ERROR_RATE),
    ]
    p = run(cascade)
    assert len(p.new_incidents) == 1
    assert p.new_incidents[0].title.endswith("(+3 services)")
    assert len(p.new_anomalies) == 4


def test_new_anomaly_next_to_an_open_incident_joins_it() -> None:
    p = run(
        [finding("api-gateway", Signal.ERROR_RATE)],
        [active("a1", "orders-service", "i1")],
        [incident("i1", {"orders-service"})],
    )
    assert p.new_incidents == []
    assert [a.incident for a in p.new_anomalies] == ["i1"]


def test_unconnected_failures_get_separate_incidents() -> None:
    # notification-service and redis-cache share no edge.
    p = run(
        [
            finding("notification-service", Signal.MEMORY, MINOR),
            finding("redis-cache", Signal.CACHE_HIT),
        ]
    )
    assert len(p.new_incidents) == 2


def test_stale_incident_is_not_reused() -> None:
    old = incident("i1", {"orders-service"}, idle=15 * 60)
    p = run([finding("orders-service")], incidents=[old])
    assert len(p.new_incidents) == 1


def test_a_quiet_anomaly_in_hysteresis_still_links_a_cascade() -> None:
    # orders stopped firing 10s ago (not yet resolved); gateway starts now.
    p = run(
        [finding("api-gateway")],
        [active("a1", "orders-service", "i1", seen_ago=10)],
        [incident("i1", {"orders-service"})],
    )
    assert [a.incident for a in p.new_anomalies] == ["i1"]


# --------------------------------------------------------------------------- lifecycle


def test_anomaly_resolves_only_after_the_hysteresis_window() -> None:
    inc = incident("i1", {"orders-service"})
    assert run([], [active("a1", "orders-service", "i1", seen_ago=10)], [inc]).resolved == []
    p = run([], [active("a1", "orders-service", "i1", seen_ago=31)], [inc])
    assert [r.anomaly_id for r in p.resolved] == ["a1"]
    assert kinds(p, "i1") == [
        IncidentEventKind.ANOMALY_RESOLVED,
        IncidentEventKind.SIGNALS_RECOVERED,
    ]
    assert p.changes["i1"].clear_signals_at == NOW


def test_recovered_open_incident_auto_resolves_after_quiet_period() -> None:
    assert not run([], [], [incident("i1", {"x"}, cleared_ago=60)]).changes.get("i1")
    p = run([], [], [incident("i1", {"x"}, cleared_ago=11 * 60)])
    assert p.changes["i1"].auto_resolve is True


@pytest.mark.parametrize(
    "status", [IncidentStatus.ACKNOWLEDGED, IncidentStatus.INVESTIGATING, IncidentStatus.MITIGATED]
)
def test_engaged_incidents_are_never_auto_resolved(status: IncidentStatus) -> None:
    p = run([], [], [incident("i1", {"x"}, status=status, cleared_ago=60 * 60)])
    assert "i1" not in p.changes or not p.changes["i1"].auto_resolve


def test_regression_after_recovery_is_recorded() -> None:
    inc = incident("i1", {"orders-service"}, cleared_ago=60, idle=60)
    p = run([finding("orders-service")], incidents=[inc])
    assert [a.incident for a in p.new_anomalies] == ["i1"]
    assert p.changes["i1"].unset_signals_cleared is True
    assert IncidentEventKind.SIGNALS_REGRESSED in kinds(p, "i1")


def test_severity_escalates_but_never_auto_deescalates() -> None:
    inc = incident("i1", {"orders-service"}, severity=IncidentSeverity.SEV3)
    p = run(
        [finding("orders-service", Signal.AVAILABILITY, CRIT, value=0.0)],
        [active("a1", "orders-service", "i1", severity=MINOR)],
        [inc],
    )
    assert p.changes["i1"].severity is IncidentSeverity.SEV1

    calm = incident("i2", {"orders-service"}, severity=IncidentSeverity.SEV1)
    p = run(
        [finding("orders-service", severity=MINOR)],
        [active("a2", "orders-service", "i2", severity=MINOR)],
        [calm],
    )
    assert "i2" not in p.changes or p.changes["i2"].severity is None


@pytest.mark.parametrize(
    ("severity", "tier", "expected"),
    [
        (CRIT, ServiceTier.CRITICAL, IncidentSeverity.SEV1),
        (CRIT, ServiceTier.INTERNAL, IncidentSeverity.SEV3),
        (MAJOR, ServiceTier.STANDARD, IncidentSeverity.SEV3),
        (MINOR, ServiceTier.CRITICAL, IncidentSeverity.SEV3),
        (MINOR, ServiceTier.INTERNAL, IncidentSeverity.SEV4),
    ],
)
def test_severity_matrix(
    severity: AnomalySeverity, tier: ServiceTier, expected: IncidentSeverity
) -> None:
    assert incident_severity([(severity, tier)]) is expected


def test_title_prefers_the_deepest_service_among_equals() -> None:
    edges = [(s.name, d.target) for s in FLEET for d in s.dependencies]
    from app.detection.correlation import dependency_depths

    depths = dependency_depths(edges)
    assert depths["postgres-primary"] == 0 and depths["api-gateway"] > depths["orders-service"]
    title = incident_title(
        [
            finding("orders-service", Signal.ERROR_RATE),
            finding("payments-service", Signal.ERROR_RATE),
        ],
        TIERS,
        depths,
    )
    assert title == "Error rate breach on payments-service (+1 service)"


def test_title_names_the_worst_observation_not_a_cause() -> None:
    title = incident_title(
        [
            finding("orders-service", severity=MINOR),
            finding("inventory-service", Signal.AVAILABILITY, CRIT, value=0.0),
        ],
        TIERS,
    )
    assert title == "inventory-service is down (+1 service)"


# --------------------------------------------------------------------------- state machine


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED),
        (IncidentStatus.OPEN, IncidentStatus.RESOLVED),
        (IncidentStatus.MITIGATED, IncidentStatus.INVESTIGATING),
        (IncidentStatus.RESOLVED, IncidentStatus.OPEN),
    ],
)
def test_allowed_transitions(current: IncidentStatus, target: IncidentStatus) -> None:
    check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IncidentStatus.INVESTIGATING, IncidentStatus.ACKNOWLEDGED),
        (IncidentStatus.RESOLVED, IncidentStatus.MITIGATED),
        (IncidentStatus.OPEN, IncidentStatus.OPEN),
    ],
)
def test_backwards_transitions_are_rejected(
    current: IncidentStatus, target: IncidentStatus
) -> None:
    with pytest.raises(InvalidTransitionError, match="allowed"):
        check_transition(current, target)

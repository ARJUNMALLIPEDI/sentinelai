"""Detection quality harness.

Replays the simulator through the production detectors and correlation rules
and measures what matters for an on-call engineer:

* **False positives:** incidents opened when nothing is wrong.
* **Time to detect (TTD):** fault injected → first incident.
* **Grouping:** one fault → how many incidents (the goal is 1).
* **Early warning:** first statistical finding → first objective breach.
* **Recovery:** fault cleared → incident signals recovered, then auto-resolved.

    python -m app.detection.evaluate                 # scenarios, seed 42
    python -m app.detection.evaluate --seeds 1 2 3   # more seeds
    python -m app.detection.evaluate --baseline-hours 6

Everything is deterministic per seed, so a tuning change shows up as a diff.
"""

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.detection.base import ServicePolicy
from app.detection.correlation import dependency_depths, neighbors_from_edges
from app.detection.pipeline import DetectionPipeline, IncidentRecord
from app.domain.enums import IncidentEventKind, IncidentSeverity, IncidentStatus
from app.simulation.engine import SimulationEngine
from app.simulation.faults import FaultType
from app.simulation.topology import FLEET

TICK = 5
START = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)  # traffic rising towards the daily peak


@dataclass(frozen=True)
class Scenario:
    name: str
    fault: FaultType
    service: str
    intensity: float = 0.8


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("DB connection leak", FaultType.DB_CONNECTION_LEAK, "orders-service", 1.0),
    Scenario("Slow queries", FaultType.SLOW_QUERIES, "postgres-primary", 0.8),
    Scenario("Bad deployment", FaultType.LATENCY_REGRESSION, "orders-service", 0.8),
    Scenario("Error burst", FaultType.ERROR_BURST, "payments-service", 0.6),
    Scenario("Service outage", FaultType.SERVICE_OUTAGE, "inventory-service", 1.0),
    Scenario("Memory leak", FaultType.MEMORY_LEAK, "payments-service", 1.0),
    Scenario("CPU saturation", FaultType.CPU_SPIKE, "auth-service", 0.8),
    Scenario("Cache degradation", FaultType.CACHE_DEGRADATION, "redis-cache", 0.9),
)


def policies() -> dict[str, ServicePolicy]:
    return {
        s.name: ServicePolicy(s.name, s.tier, s.slo_latency_p95_ms, s.slo_error_rate) for s in FLEET
    }


def new_pipeline() -> DetectionPipeline:
    edges = [(s.name, d.target) for s in FLEET for d in s.dependencies]
    return DetectionPipeline(policies(), neighbors_from_edges(edges), dependency_depths(edges))


@dataclass
class ScenarioResult:
    scenario: str
    seed: int
    false_positives: int
    ttd_s: float | None
    incidents: int
    services: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    severity: IncidentSeverity | None = None
    title: str = ""
    early_warning_s: float | None = None
    recovered_after_clear_s: float | None = None
    auto_resolved: bool = False


def _run(
    engine: SimulationEngine, pipe: DetectionPipeline, start: datetime, ticks: int
) -> datetime:
    t = start
    for _ in range(ticks):
        pipe.observe(engine.tick(t).samples)
        pipe.step(t)
        t += timedelta(seconds=TICK)
    return t


def run_scenario(
    scenario: Scenario,
    seed: int = 42,
    warmup_min: int = 35,
    fault_min: int = 10,
    recovery_min: int = 15,
) -> ScenarioResult:
    engine, pipe = SimulationEngine(seed=seed, tick_seconds=TICK), new_pipeline()
    t = _run(engine, pipe, START, warmup_min * 60 // TICK)
    before = set(pipe.store.incidents)

    injected_at = t
    # Duration outlives the fault window so the harness controls when it ends.
    fault = engine.inject_fault(
        scenario.fault, scenario.service, t, scenario.intensity, duration_s=(fault_min + 5) * 60
    )
    t = _run(engine, pipe, t, fault_min * 60 // TICK)
    during = [r for k, r in pipe.store.incidents.items() if k not in before]

    cleared_at = t
    engine.clear_fault(fault.id)
    _run(engine, pipe, t, recovery_min * 60 // TICK)

    result = ScenarioResult(scenario.name, seed, len(before), None, len(during))
    if not during:
        return result
    first: IncidentRecord = min(during, key=lambda r: r.state.opened_at)
    result.ttd_s = (first.state.opened_at - injected_at).total_seconds()
    result.title = first.title
    result.severity = min(r.max_severity for r in during)
    result.services = sorted(set().union(*(r.state.services for r in during)))
    result.signals = sorted({f"{s}:{sig}" for r in during for s, sig, _ in r.signals})

    detections = [
        (ts, e.data) for ts, e in first.events if e.kind is IncidentEventKind.ANOMALY_DETECTED
    ]
    stat = [ts for ts, d in detections if d.get("detector") == "robust_zscore"]
    thresh = [ts for ts, d in detections if d.get("detector") == "threshold"]
    if stat and thresh:
        result.early_warning_s = (min(thresh) - min(stat)).total_seconds()

    recovered = [
        ts
        for ts, e in first.events
        if e.kind is IncidentEventKind.SIGNALS_RECOVERED and ts >= cleared_at
    ]
    if recovered:
        result.recovered_after_clear_s = (recovered[-1] - cleared_at).total_seconds()
    result.auto_resolved = first.state.status is IncidentStatus.RESOLVED
    return result


def run_baseline(seed: int, hours: float, start: datetime = START) -> list[IncidentRecord]:
    """Fault-free run; every incident it opens is a false positive."""
    engine, pipe = SimulationEngine(seed=seed, tick_seconds=TICK), new_pipeline()
    _run(engine, pipe, start, int(hours * 3600 // TICK))
    return list(pipe.store.incidents.values())


def _fmt_s(x: float | None) -> str:
    return "—" if x is None else f"{x:.0f}s"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--seeds", type=int, nargs="+", default=[42])
    parser.add_argument("--baseline-hours", type=float, default=4.0)
    args = parser.parse_args()

    print(f"\nBaseline (no faults), {args.baseline_hours:g}h per seed:")
    for seed in args.seeds:
        for hour in (3, 15):  # trough and peak of the daily cycle
            start = START.replace(hour=hour)
            fps = run_baseline(seed, args.baseline_hours, start)
            print(
                f"  seed {seed:>3}, start {hour:02d}:00 UTC → {len(fps)} false-positive incident(s)"
                + "".join(f"\n      {r.state.opened_at:%H:%M:%S} {r.title}" for r in fps)
            )

    header = (
        f"{'scenario':<20} {'seed':>4} {'TTD':>6} {'#inc':>4} {'sev':>4} {'early':>6} "
        f"{'recov':>6} {'auto':>4}  first incident"
    )
    print("\n" + header + "\n" + "-" * len(header))
    for seed in args.seeds:
        for sc in SCENARIOS:
            r = run_scenario(sc, seed)
            print(
                f"{r.scenario:<20} {seed:>4} {_fmt_s(r.ttd_s):>6} {r.incidents:>4} "
                f"{(r.severity.value if r.severity else '—'):>4} {_fmt_s(r.early_warning_s):>6} "
                f"{_fmt_s(r.recovered_after_clear_s):>6} {'yes' if r.auto_resolved else 'no':>4}  "
                f"{r.title}  [fp={r.false_positives}]"
            )
    print(
        "\nTTD: injection → first incident. early: first statistical finding → first SLO/"
        "capacity breach.\nrecov: fault cleared → all signals normal. auto: auto-resolved "
        "after 10 quiet minutes."
    )


if __name__ == "__main__":
    main()

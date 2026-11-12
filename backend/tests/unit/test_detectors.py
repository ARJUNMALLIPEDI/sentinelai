import random
from datetime import UTC, datetime, timedelta

import pytest

from app.detection.base import ServicePolicy, ServiceSeries
from app.detection.signals import Signal
from app.detection.statistical import RobustZScoreDetector
from app.detection.threshold import ThresholdDetector
from app.domain.enums import AnomalySeverity, ServiceTier

T0 = datetime(2026, 11, 11, 18, 0, tzinfo=UTC)
POLICY = ServicePolicy(
    "orders-service", ServiceTier.CRITICAL, slo_latency_p95_ms=400, slo_error_rate=0.02
)
BASELINE = {
    Signal.LATENCY_P95: 250.0,
    Signal.ERROR_RATE: 0.003,
    Signal.CPU: 40.0,
    Signal.MEMORY: 50.0,
    Signal.RPS: 110.0,
    Signal.DB_POOL: 0.25,
    Signal.CACHE_HIT: 0.95,
    Signal.AVAILABILITY: 1.0,
}
NOISE = {
    Signal.LATENCY_P95: 15.0,
    Signal.ERROR_RATE: 0.001,
    Signal.CPU: 2.5,
    Signal.MEMORY: 1.0,
    Signal.RPS: 4.0,
    Signal.DB_POOL: 0.02,
    Signal.CACHE_HIT: 0.004,
    Signal.AVAILABILITY: 0.0,
}


def series(
    n: int = 400, overrides: dict[Signal, list[float | None]] | None = None, seed: int = 1
) -> ServiceSeries:
    """``n`` noisy baseline samples; ``overrides`` replaces the *last* values of a signal."""
    rng = random.Random(seed)
    values: dict[Signal, list[float | None]] = {
        s: [BASELINE[s] + rng.gauss(0, NOISE[s]) for _ in range(n)] for s in Signal
    }
    for signal, tail in (overrides or {}).items():
        values[signal] = values[signal][: n - len(tail)] + tail
    return ServiceSeries(
        "orders-service", [T0 + timedelta(seconds=5 * i) for i in range(n)], values
    )


def signals(findings: list) -> set[Signal]:  # type: ignore[type-arg]
    return {f.signal for f in findings}


# --------------------------------------------------------------------------- statistical

STAT = RobustZScoreDetector()


def test_noise_alone_never_fires() -> None:
    for seed in range(20):
        assert STAT.detect(series(seed=seed), POLICY) == []


def test_a_sustained_step_fires_with_baseline_and_score() -> None:
    [finding] = STAT.detect(series(overrides={Signal.LATENCY_P95: [600.0, 640.0, 610.0]}), POLICY)
    assert finding.signal is Signal.LATENCY_P95 and finding.severity is AnomalySeverity.MINOR
    assert finding.baseline == pytest.approx(250, abs=10)
    assert finding.score is not None and finding.score > 4
    assert "610ms above its recent baseline" in finding.message


def test_needs_every_confirmation_sample() -> None:
    # Two high points then one normal one: a blip, not an anomaly.
    assert STAT.detect(series(overrides={Signal.LATENCY_P95: [600.0, 640.0, 250.0]}), POLICY) == []


def test_no_verdict_during_warm_up() -> None:
    short = series(n=40, overrides={Signal.LATENCY_P95: [900.0, 900.0, 900.0]})
    assert STAT.detect(short, POLICY) == []


def test_huge_z_but_tiny_change_is_ignored() -> None:
    # 0.3% -> 0.7% errors is many MADs away from a quiet baseline, but not worth a page.
    assert STAT.detect(series(overrides={Signal.ERROR_RATE: [0.007] * 3}), POLICY) == []
    assert signals(STAT.detect(series(overrides={Signal.ERROR_RATE: [0.05] * 3}), POLICY)) == {
        Signal.ERROR_RATE
    }


@pytest.mark.parametrize(
    ("signal", "tail", "fires"),
    [
        (Signal.LATENCY_P95, [60.0] * 3, False),  # faster is not an incident
        (Signal.CACHE_HIT, [0.40] * 3, True),  # hit rate collapsing is
        (Signal.CACHE_HIT, [0.999] * 3, False),
        (Signal.RPS, [20.0] * 3, True),  # traffic vanishing: both directions matter
        (Signal.RPS, [400.0] * 3, True),
        (Signal.DB_POOL, [0.9] * 3, True),
    ],
)
def test_direction_is_respected(signal: Signal, tail: list[float], fires: bool) -> None:
    found = signals(STAT.detect(series(overrides={signal: tail}), POLICY))  # type: ignore[dict-item]
    assert (signal in found) is fires


def test_robust_baseline_is_not_masked_by_an_earlier_spike() -> None:
    # A 2-minute spike 20 minutes ago inflates the std-dev a lot, but not the MAD.
    s = series()
    lat = s.values[Signal.LATENCY_P95]
    for i in range(150, 174):
        lat[i] = 3000.0
    lat[-3:] = [560.0, 570.0, 565.0]
    assert Signal.LATENCY_P95 in signals(STAT.detect(s, POLICY))


def test_missing_values_are_skipped_not_guessed() -> None:
    s = series(overrides={Signal.DB_POOL: [None, None, None]})
    assert Signal.DB_POOL not in signals(STAT.detect(s, POLICY))


# --------------------------------------------------------------------------- threshold

THRESH = ThresholdDetector()


def test_slo_breach_needs_three_consecutive_samples() -> None:
    assert THRESH.detect(series(overrides={Signal.LATENCY_P95: [500.0, 500.0]}), POLICY) == []
    [f] = THRESH.detect(series(overrides={Signal.LATENCY_P95: [500.0, 520.0, 510.0]}), POLICY)
    assert f.severity is AnomalySeverity.MAJOR and f.threshold == 400
    assert "above SLO (400ms) for 3 samples" in f.message


def test_outage_fires_immediately_and_as_critical() -> None:
    [f] = THRESH.detect(series(overrides={Signal.AVAILABILITY: [0.0]}), POLICY)
    assert f.signal is Signal.AVAILABILITY and f.severity is AnomalySeverity.CRITICAL


def test_rolling_deploy_does_not_page_but_a_longer_loss_does() -> None:
    deploy = series(overrides={Signal.AVAILABILITY: [2 / 3, 2 / 3, 1.0]})
    assert THRESH.detect(deploy, POLICY) == []
    oom = series(overrides={Signal.AVAILABILITY: [2 / 3] * 4})
    assert [f.severity for f in THRESH.detect(oom, POLICY)] == [AnomalySeverity.MAJOR]


def test_only_the_most_severe_rule_per_signal_is_reported() -> None:
    [f] = THRESH.detect(series(overrides={Signal.ERROR_RATE: [0.6, 0.6, 0.6]}), POLICY)
    assert f.severity is AnomalySeverity.CRITICAL


def test_threshold_uses_each_services_own_slo() -> None:
    lenient = ServicePolicy("notification-service", ServiceTier.INTERNAL, 2000, 0.05)
    assert THRESH.detect(series(overrides={Signal.LATENCY_P95: [500.0] * 3}), lenient) == []

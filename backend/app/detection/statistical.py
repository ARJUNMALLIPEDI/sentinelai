"""Robust z-score detection: "is this signal far from its own recent normal?"

For each signal the baseline is the preceding ``baseline_samples`` points
(30 minutes at 5 s ticks), excluding the points being judged. The score is

    z = (x - median) / (1.4826 · MAD)

where MAD is the median absolute deviation. 1.4826 scales MAD to match the
standard deviation for normally distributed data.

Why median/MAD instead of mean/standard deviation: a past spike inside the
baseline window inflates the standard deviation and *masks* the next anomaly.
Median and MAD barely move when a few points are extreme (50% breakdown
point), so the baseline describes what is normal, not what happened last time.

A finding needs **all** of the last ``confirm_samples`` points to pass every
guard — ``|z| ≥ z_threshold`` in the bad direction, plus the minimum absolute
and relative changes from ``SignalSpec``. Confirmation trades a few seconds of
detection delay for not paging on a single noisy sample.

Known limitation: if an anomaly persists longer than about half the baseline
window, the median absorbs it ("new normal") and this detector stops firing.
Threshold rules still hold anything that breaches an objective. Freezing the
baseline while an anomaly is active is a straightforward extension.
"""

import statistics
from dataclasses import dataclass

from app.detection.base import Finding, ServicePolicy, ServiceSeries
from app.detection.signals import SIGNALS, Direction, SignalSpec
from app.domain.enums import AnomalySeverity

MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class RobustZScoreDetector:
    baseline_samples: int = 360
    min_baseline_samples: int = 60  # warm-up: no verdicts on less than 5 minutes of history
    confirm_samples: int = 3
    z_threshold: float = 4.0
    name: str = "robust_zscore"

    @property
    def window_samples(self) -> int:
        return self.baseline_samples + self.confirm_samples

    def detect(self, series: ServiceSeries, policy: ServicePolicy) -> list[Finding]:
        findings = []
        for spec in SIGNALS.values():
            if spec.statistical:
                finding = self._check(spec, series, policy)
                if finding:
                    findings.append(finding)
        return findings

    def _check(
        self, spec: SignalSpec, series: ServiceSeries, policy: ServicePolicy
    ) -> Finding | None:
        values = series.values[spec.signal]
        recent = values[-self.confirm_samples :]
        if len(recent) < self.confirm_samples or any(v is None for v in recent):
            return None
        baseline = [
            v for v in values[-self.window_samples : -self.confirm_samples] if v is not None
        ]
        if len(baseline) < self.min_baseline_samples:
            return None

        median = statistics.median(baseline)
        mad = statistics.median(abs(v - median) for v in baseline)
        scale = max(MAD_TO_SIGMA * mad, spec.mad_floor)

        scores = []
        for v in recent:
            assert v is not None
            deviation = v - median
            if spec.direction is Direction.UP and deviation <= 0:
                return None
            if spec.direction is Direction.DOWN and deviation >= 0:
                return None
            z = deviation / scale
            if abs(z) < self.z_threshold or abs(deviation) < spec.min_abs_change:
                return None
            if median != 0 and abs(deviation) < spec.min_rel_change * abs(median):
                return None
            scores.append(z)

        value = recent[-1]
        assert value is not None
        word = "above" if value > median else "below"
        return Finding(
            service=policy.name,
            signal=spec.signal,
            detector=self.name,
            severity=AnomalySeverity.MINOR,
            observed_at=series.ts[-1],
            value=value,
            baseline=median,
            score=round(scores[-1], 2),
            message=(
                f"{policy.name}: {spec.title} {spec.fmt(value)} {word} its recent "
                f"baseline {spec.fmt(median)} (z={scores[-1]:.1f})"
            ),
        )

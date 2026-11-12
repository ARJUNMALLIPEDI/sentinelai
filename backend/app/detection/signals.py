"""The signals detection looks at, and how each one is allowed to move.

Every statistical check needs three guards besides the z-score itself:

* **direction** — latency going *down* is good news, not an incident;
* **minimum absolute change** — an error rate moving from 0.1% to 0.4% has an
  enormous z-score (the baseline barely varies) but nobody should be paged;
* **MAD floor** — a near-constant baseline has a MAD of ~0, which would turn
  any wiggle into an infinite z-score.

These numbers are the detector's "taste". They were tuned against the
evaluation harness (``app/detection/evaluate.py``), not guessed.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.domain.enums import Signal

__all__ = ["SIGNALS", "Direction", "Signal", "SignalSpec"]


class Direction(StrEnum):
    UP = "up"  # only increases are bad
    DOWN = "down"  # only decreases are bad
    BOTH = "both"


@dataclass(frozen=True)
class SignalSpec:
    signal: Signal
    title: str
    direction: Direction
    min_abs_change: float
    min_rel_change: float  # fraction of the baseline
    mad_floor: float
    statistical: bool = True

    def fmt(self, value: float) -> str:
        if self.signal is Signal.LATENCY_P95:
            return f"{value:.0f}ms"
        if self.signal in (
            Signal.ERROR_RATE,
            Signal.DB_POOL,
            Signal.CACHE_HIT,
            Signal.AVAILABILITY,
        ):
            return f"{value:.1%}"
        if self.signal in (Signal.CPU, Signal.MEMORY):
            return f"{value:.0f}%"
        return f"{value:.0f}/s"


SIGNALS: dict[Signal, SignalSpec] = {
    s.signal: s
    for s in (
        SignalSpec(Signal.LATENCY_P95, "p95 latency", Direction.UP, 25.0, 0.5, 2.0),
        SignalSpec(Signal.ERROR_RATE, "error rate", Direction.UP, 0.01, 1.0, 0.001),
        SignalSpec(Signal.CPU, "CPU", Direction.UP, 12.0, 0.3, 1.0),
        SignalSpec(Signal.MEMORY, "memory", Direction.UP, 10.0, 0.15, 0.5),
        SignalSpec(Signal.RPS, "request rate", Direction.BOTH, 0.0, 0.4, 1.0),
        SignalSpec(Signal.DB_POOL, "DB pool utilization", Direction.UP, 0.15, 0.5, 0.02),
        SignalSpec(Signal.CACHE_HIT, "cache hit rate", Direction.DOWN, 0.1, 0.1, 0.005),
        # Instance availability is a hard fact, not a distribution: threshold rules only.
        SignalSpec(
            Signal.AVAILABILITY, "availability", Direction.DOWN, 0.0, 0.0, 0.0, statistical=False
        ),
    )
}

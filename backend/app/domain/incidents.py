"""Incident status state machine.

    open ──► acknowledged ──► investigating ──► mitigated ──► resolved
      │            │                 ▲              │             │
      └────────────┴─────────────────┴──────────────┘             │
                     (forward skips allowed)                       │
    resolved ──► open   (reopen)  ◄──────────────────────────────┘

``mitigated → investigating`` is allowed because a mitigation can fail.
Everything else that goes backwards is rejected, so the timeline stays a
truthful record of how the incident progressed.
"""

from app.domain.enums import IncidentStatus as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.OPEN: frozenset({S.ACKNOWLEDGED, S.INVESTIGATING, S.MITIGATED, S.RESOLVED}),
    S.ACKNOWLEDGED: frozenset({S.INVESTIGATING, S.MITIGATED, S.RESOLVED}),
    S.INVESTIGATING: frozenset({S.MITIGATED, S.RESOLVED}),
    S.MITIGATED: frozenset({S.INVESTIGATING, S.RESOLVED}),
    S.RESOLVED: frozenset({S.OPEN}),
}


class InvalidTransitionError(ValueError):
    pass


def check_transition(current: S, target: S) -> None:
    if target not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(t.value for t in TRANSITIONS[current]))
        raise InvalidTransitionError(
            f"cannot move an incident from {current.value} to {target.value} (allowed: {allowed})"
        )

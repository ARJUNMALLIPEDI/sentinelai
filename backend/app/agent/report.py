"""The incident report: what the agent must produce, and how it is checked.

The report arrives as the input of the ``generate_incident_report`` tool, so
its *shape* is enforced by the API's strict tool schema. Pydantic then enforces
what JSON Schema strict mode can't (string lengths, numeric ranges), and
``grounding_errors`` enforces what no schema can:

* every cited evidence ID was actually issued by a tool in *this* investigation;
* every confirmed finding cites at least one piece of evidence;
* a root cause marked ``confirmed`` must be backed by evidence and can't be
  low confidence;
* services named in the report exist.

A report that fails any check is sent back to the model as a tool error listing
the problems, so it can correct itself. Invented evidence is never stored.
"""

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import IncidentSeverity


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RemediationAction(StrEnum):
    """Actions the remediation engine (Milestone 5) knows how to execute after approval."""

    RESTART_SERVICE = "restart_service"
    SCALE_SERVICE = "scale_service"
    CLEAR_CACHE = "clear_cache"
    ADJUST_RESOURCES = "adjust_resources"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Evidence = list[str]
EVIDENCE_DESC = 'IDs of tool results that support this, e.g. ["E2", "E5"]'


class TimelineEntry(_Strict):
    at: datetime = Field(description="When it happened (ISO 8601), taken from tool results")
    event: str = Field(max_length=300)
    evidence: Evidence = Field(description=EVIDENCE_DESC)


class MetricObservation(_Strict):
    service: str
    signal: str = Field(max_length=40, description="e.g. latency_p95_ms, error_rate, db_pool")
    observation: str = Field(max_length=300, description="What changed, with the numbers")
    evidence: Evidence = Field(min_length=1, description=EVIDENCE_DESC)


class LogExcerpt(_Strict):
    service: str
    excerpt: str = Field(max_length=500, description="Quoted log message(s), verbatim")
    evidence: Evidence = Field(min_length=1, description=EVIDENCE_DESC)


class Finding(_Strict):
    statement: str = Field(max_length=500, description="A fact directly shown by the evidence")
    evidence: Evidence = Field(min_length=1, description=EVIDENCE_DESC)


class Hypothesis(_Strict):
    statement: str = Field(max_length=500, description="A plausible explanation not yet proven")
    confidence: Confidence
    evidence: Evidence = Field(description=EVIDENCE_DESC)
    how_to_verify: str = Field(max_length=300, description="What would confirm or refute it")


class RootCause(_Strict):
    statement: str = Field(max_length=600)
    status: Literal["confirmed", "probable", "undetermined"] = Field(
        description="confirmed only if the evidence shows it directly"
    )
    confidence: Confidence
    evidence: Evidence = Field(description=EVIDENCE_DESC)


class Remediation(_Strict):
    action: RemediationAction
    target_service: str
    replicas: int | None = Field(
        default=None, ge=1, le=20, description="Only for scale_service: desired instance count"
    )
    rationale: str = Field(max_length=500)
    risk: Confidence = Field(description="Operational risk of performing this action")
    evidence: Evidence = Field(min_length=1, description=EVIDENCE_DESC)


class SeparateIssue(_Strict):
    """Detection groups by topology and can merge unrelated failures; the agent can say so."""

    statement: str = Field(max_length=500)
    services: list[str] = Field(min_length=1)
    evidence: Evidence = Field(min_length=1, description=EVIDENCE_DESC)


class IncidentReport(_Strict):
    summary: str = Field(max_length=2000, description="Plain-language summary for responders")
    severity: IncidentSeverity = Field(description="Your assessment of the impact")
    affected_services: list[str] = Field(min_length=1)
    timeline: list[TimelineEntry] = Field(min_length=1)
    key_metrics: list[MetricObservation]
    log_evidence: list[LogExcerpt]
    confirmed_findings: list[Finding]
    hypotheses: list[Hypothesis]
    root_cause: RootCause
    recommended_remediations: list[Remediation]
    separate_issues: list[SeparateIssue] = Field(
        description="Unrelated problems that were grouped into this incident, if any"
    )
    overall_confidence: Confidence

    def cited_evidence(self) -> set[str]:
        ids: set[str] = set()
        for group in (
            self.timeline,
            self.key_metrics,
            self.log_evidence,
            self.confirmed_findings,
            self.hypotheses,
            self.recommended_remediations,
            self.separate_issues,
        ):
            for item in group:
                ids.update(item.evidence)
        ids.update(self.root_cause.evidence)
        return ids

    def services_named(self) -> set[str]:
        names = set(self.affected_services)
        names.update(m.service for m in self.key_metrics)
        names.update(entry.service for entry in self.log_evidence)
        names.update(r.target_service for r in self.recommended_remediations)
        for issue in self.separate_issues:
            names.update(issue.services)
        return names


def grounding_errors(
    report: IncidentReport, issued_evidence: Iterable[str], known_services: Iterable[str]
) -> list[str]:
    """Problems that make the report untrustworthy. Empty list = accepted."""
    issued, known = set(issued_evidence), set(known_services)
    errors = []
    invented = sorted(report.cited_evidence() - issued)
    if invented:
        errors.append(
            f"cites evidence IDs that were never returned by a tool: {', '.join(invented)}. "
            f"Valid IDs: {', '.join(sorted(issued, key=_evidence_order)) or 'none yet'}"
        )
    unknown = sorted(report.services_named() - known)
    if unknown:
        errors.append(f"names services that do not exist: {', '.join(unknown)}")
    rc = report.root_cause
    if rc.status == "confirmed" and not rc.evidence:
        errors.append("root_cause.status is 'confirmed' but it cites no evidence")
    if rc.status == "confirmed" and rc.confidence is Confidence.LOW:
        errors.append("a confirmed root cause cannot have low confidence; use 'probable'")
    for i, r in enumerate(report.recommended_remediations):
        if r.action is RemediationAction.SCALE_SERVICE and r.replicas is None:
            errors.append(f"recommended_remediations[{i}]: scale_service requires replicas")
        if r.action is not RemediationAction.SCALE_SERVICE and r.replicas is not None:
            errors.append(f"recommended_remediations[{i}]: replicas only applies to scale_service")
    return errors


def _evidence_order(eid: str) -> int:
    return int(eid[1:]) if eid[1:].isdigit() else 0

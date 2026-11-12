"""Enums shared by the persistence layer, the API and the simulator.

Kept dependency-free so any layer can import them without pulling in
SQLAlchemy or the simulation engine.
"""

from enum import StrEnum


class ServiceTier(StrEnum):
    """How customer-facing a service is; drives default incident severity later."""

    CRITICAL = "critical"
    STANDARD = "standard"
    INTERNAL = "internal"


class ServiceKind(StrEnum):
    API = "api"
    DATABASE = "database"
    CACHE = "cache"


class LogLevel(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class DeploymentKind(StrEnum):
    DEPLOY = "deploy"
    ROLLBACK = "rollback"


class AnomalySeverity(StrEnum):
    MINOR = "minor"  # statistically unusual, still within objectives
    MAJOR = "major"  # objective breached (SLO, capacity)
    CRITICAL = "critical"  # service down or most requests failing


class AnomalyStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"


class IncidentSeverity(StrEnum):
    """SEV1 is the worst. Ordered so that min() over values gives the most severe."""

    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class IncidentStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"


class IncidentEventKind(StrEnum):
    OPENED = "opened"
    ANOMALY_DETECTED = "anomaly_detected"
    ANOMALY_RESOLVED = "anomaly_resolved"
    SEVERITY_CHANGED = "severity_changed"
    STATUS_CHANGED = "status_changed"
    SIGNALS_RECOVERED = "signals_recovered"
    SIGNALS_REGRESSED = "signals_regressed"
    NOTE = "note"
    INVESTIGATION_STARTED = "investigation_started"
    INVESTIGATION_COMPLETED = "investigation_completed"
    INVESTIGATION_FAILED = "investigation_failed"


class Signal(StrEnum):
    """Signals detection evaluates (derived ones: pool utilization, availability)."""

    LATENCY_P95 = "latency_p95_ms"
    ERROR_RATE = "error_rate"
    CPU = "cpu_pct"
    MEMORY = "memory_pct"
    RPS = "rps"
    DB_POOL = "db_pool_utilization"
    CACHE_HIT = "cache_hit_rate"
    AVAILABILITY = "availability"


class InvestigationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"  # a validated report was produced
    INCOMPLETE = "incomplete"  # a budget ran out; a partial report was stored
    FAILED = "failed"  # the model or the platform errored

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

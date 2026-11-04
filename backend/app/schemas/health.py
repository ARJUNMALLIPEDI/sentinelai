from typing import Literal

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class DependencyCheck(BaseModel):
    status: Literal["ok", "error"]
    latency_ms: float
    # Exception class name only — full messages can contain hostnames/credentials.
    error: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, DependencyCheck]

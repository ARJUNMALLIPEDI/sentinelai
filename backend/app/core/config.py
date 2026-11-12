"""Application settings, loaded from environment variables (and a local .env file).

Every tunable lives here so nothing is read from ``os.environ`` ad hoc elsewhere.
Secrets are typed as ``SecretStr`` so they never appear in logs or reprs.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- application ---
    app_name: str = "SentinelAI"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # --- persistence ---
    database_url: str
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    redis_url: str

    # --- health checks ---
    health_check_timeout_s: float = Field(default=2.0, gt=0, le=30)

    # --- simulation / worker ---
    sim_seed: int = 42
    sim_tick_seconds: int = Field(default=5, ge=1, le=60)
    sim_backfill_minutes: int = Field(default=60, ge=0, le=24 * 60)
    telemetry_retention_hours: int = Field(default=24, ge=1, le=24 * 30)
    # Fault injection endpoints exist for demos and tests; they are never mounted
    # in production regardless of this flag.
    simulation_control_enabled: bool = True
    worker_metrics_port: int = Field(default=9100, ge=1024, le=65535)

    # --- detection / incidents ---
    # An untouched incident whose signals have been normal this long is resolved
    # automatically. Acknowledged incidents are never auto-resolved.
    incident_auto_resolve_minutes: int = Field(default=10, ge=1, le=24 * 60)

    @property
    def snapshot_ttl_s(self) -> int:
        """A service whose latest snapshot is older than three ticks is "unknown"."""
        return self.sim_tick_seconds * 3

    @property
    def simulation_control_mounted(self) -> bool:
        return self.simulation_control_enabled and self.environment != "production"

    # --- AI agent (used from Milestone 4) ---
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        """Accept plain ``postgresql://`` URLs but always use the asyncpg driver.

        Mixing a sync driver into an async engine fails at first query, which is a
        confusing error far from its cause; normalising here fails fast instead.
        """
        if v.startswith("postgresql://") or v.startswith("postgres://"):
            v = "postgresql+asyncpg://" + v.split("://", 1)[1]
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL (postgresql+asyncpg://...)")
        return v

    @field_validator("sim_tick_seconds")
    @classmethod
    def _tick_divides_a_minute(cls, v: int) -> int:
        # Ticks are scheduled on wall-clock seconds (0, 5, 10, ...), so the
        # interval has to divide 60 to stay evenly spaced across minutes.
        if 60 % v:
            raise ValueError("SIM_TICK_SECONDS must divide 60 (e.g. 1, 2, 5, 10, 15, 30)")
        return v

    @field_validator("redis_url")
    @classmethod
    def _require_redis_scheme(cls, v: str) -> str:
        if not v.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must start with redis:// or rediss://")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields (DATABASE_URL, REDIS_URL) come from the environment

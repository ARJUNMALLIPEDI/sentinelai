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

    @field_validator("redis_url")
    @classmethod
    def _require_redis_scheme(cls, v: str) -> str:
        if not v.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must start with redis:// or rediss://")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields (DATABASE_URL, REDIS_URL) come from the environment

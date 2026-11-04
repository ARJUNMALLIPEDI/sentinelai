"""Import every model here so Alembic's autogenerate sees the full metadata."""

from app.models.service import Service, ServiceTier

__all__ = ["Service", "ServiceTier"]

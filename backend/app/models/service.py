import uuid

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.types import str_enum
from app.domain.enums import ServiceKind, ServiceTier


class Service(TimestampMixin, Base):
    """A service in the monitored fleet, with the objectives it is judged against."""

    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint("desired_instances >= 0", name="desired_instances_non_negative"),
        CheckConstraint("slo_error_rate > 0 AND slo_error_rate < 1", name="slo_error_rate_range"),
        CheckConstraint("slo_latency_p95_ms > 0", name="slo_latency_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[ServiceKind] = mapped_column(
        str_enum(ServiceKind, "service_kind"), nullable=False, default=ServiceKind.API
    )
    tier: Mapped[ServiceTier] = mapped_column(
        str_enum(ServiceTier, "service_tier"), nullable=False, default=ServiceTier.STANDARD
    )
    description: Mapped[str | None] = mapped_column(Text)
    desired_instances: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    slo_latency_p95_ms: Mapped[float] = mapped_column(Float, nullable=False)
    slo_error_rate: Mapped[float] = mapped_column(Float, nullable=False)
    current_version: Mapped[str | None] = mapped_column(String(32))

    dependencies: Mapped[list["ServiceDependency"]] = relationship(
        foreign_keys="ServiceDependency.service_id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class ServiceDependency(Base):
    """Directed edge: ``service`` calls ``depends_on``.

    Stored (not just kept in simulator code) because investigations need to
    walk the graph: "orders is slow — what does it call, and who calls it?"
    """

    __tablename__ = "service_dependencies"
    __table_args__ = (
        CheckConstraint("service_id <> depends_on_id", name="no_self_dependency"),
        CheckConstraint("criticality >= 0 AND criticality <= 1", name="criticality_range"),
        CheckConstraint("calls_per_request > 0", name="calls_positive"),
    )

    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    calls_per_request: Mapped[float] = mapped_column(Float, nullable=False)
    criticality: Mapped[float] = mapped_column(Float, nullable=False)
    is_async: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    depends_on: Mapped[Service] = relationship(foreign_keys=[depends_on_id], lazy="joined")

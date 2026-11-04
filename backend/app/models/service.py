import enum
import uuid

from sqlalchemy import CheckConstraint, Enum, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ServiceTier(enum.StrEnum):
    """How customer-facing a service is; drives default incident severity later."""

    CRITICAL = "critical"
    STANDARD = "standard"
    INTERNAL = "internal"


class Service(TimestampMixin, Base):
    """A (simulated) service in the monitored fleet."""

    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint("desired_instances >= 0", name="desired_instances_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    tier: Mapped[ServiceTier] = mapped_column(
        # VARCHAR + CHECK instead of a native PG enum: adding values later is a
        # normal transactional migration (see docs/ARCHITECTURE.md §5).
        # create_constraint=True is required — SQLAlchemy 2.x omits the CHECK by default.
        Enum(
            ServiceTier,
            native_enum=False,
            create_constraint=True,
            length=16,
            name="service_tier",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ServiceTier.STANDARD,
    )
    description: Mapped[str | None] = mapped_column(Text)
    desired_instances: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

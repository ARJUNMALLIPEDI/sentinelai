from enum import StrEnum

from sqlalchemy import Enum


def str_enum(enum_cls: type[StrEnum], name: str, length: int = 16) -> Enum:
    """VARCHAR + CHECK enum column type (see docs/ARCHITECTURE.md §5).

    Not a native Postgres ENUM: adding a value later is an ordinary
    transactional migration. ``create_constraint=True`` is required because
    SQLAlchemy 2.x omits the CHECK by default.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        length=length,
        name=name,
        values_callable=lambda e: [m.value for m in e],
    )

"""Declarative base, constraint naming convention and shared column helpers."""

import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from switchyard.ids import uuid7

#: Deterministic constraint names keep Alembic autogenerate and the migrations stable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class of every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class EntityMixin:
    """Columns every object table shares: UUIDv7 ``id`` and optimistic-locking ``version``."""

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class TenantOwnedMixin(EntityMixin):
    """Entity owned by one tenant, with a name that is unique per tenant.

    Subclasses add ``tenant_unique()`` to ``__table_args__``.
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)


def enum_check(column: str, values: type[enum.StrEnum]) -> CheckConstraint:
    """``CHECK (column IN (...))`` for a string enumeration (ADR 0005: enums are text)."""
    allowed = ", ".join(f"'{m.value}'" for m in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


def tenant_unique() -> tuple[UniqueConstraint, UniqueConstraint]:
    """Return the two unique constraints every tenant-owned table needs.

    Returns:
        ``(tenant_id, name)`` for per-tenant names and ``(tenant_id, id)`` as the target of the
        composite foreign keys built by :func:`tenant_fk`.
    """
    return UniqueConstraint("tenant_id", "name"), UniqueConstraint("tenant_id", "id")


def tenant_fk(column: str, target: str, *, cascade: bool = False) -> ForeignKeyConstraint:
    """Composite foreign key ``(tenant_id, column) -> target(tenant_id, id)``.

    A reference can therefore never cross tenants, even for a session that bypasses RLS.

    Args:
        column: Referencing column on this table.
        target: Referenced table name.
        cascade: Delete this row when the referenced row is deleted (child tables).
    """
    return ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        ondelete="CASCADE" if cascade else None,
    )

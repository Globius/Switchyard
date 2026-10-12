"""SQLAlchemy models for GSLB: zones and wide IPs (ADR 0160).

Both tables are tenant-owned with RLS. A zone is created by a system administrator for one tenant
(a zone name is unique across tenants, so nobody can take over another's); the tenant then manages
the wide IPs inside it.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from switchyard.db.base import Base, TenantOwnedMixin


def _timestamp(*, nullable: bool = True, default_now: bool = False) -> Mapped[datetime]:
    return mapped_column(
        TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=text("now()") if default_now else None,
    )


class GslbZoneRow(TenantOwnedMixin, Base):
    """``gslb_zone``: a DNS zone hosted on the controller's PowerDNS and owned by one tenant."""

    __tablename__ = "gslb_zone"
    __table_args__ = (
        UniqueConstraint("name"),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
    )

    nameservers: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    dnssec: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class GslbWideIpRow(TenantOwnedMixin, Base):
    """``gslb_wide_ip``: a name in a zone whose answer follows the health of its members."""

    __tablename__ = "gslb_wide_ip"
    __table_args__ = (
        UniqueConstraint("zone_id", "name"),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["tenant_id", "zone_id"], ["gslb_zone.tenant_id", "gslb_zone.id"], ondelete="CASCADE"
        ),
        CheckConstraint("version >= 1", name="version"),
    )

    zone_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: ``algorithm``, ``ttl``, ``health`` and ``members`` (see :mod:`switchyard.domain.gslb`).
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: Hash of the records last written to PowerDNS; the sync job writes again only when it changes.
    applied_hash: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = _timestamp()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)

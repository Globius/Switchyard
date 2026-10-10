"""SQLAlchemy models for the M1 tables.

Conventions (see ADR 0002):

- Every object has a UUIDv7 ``id`` and an integer ``version``.
- Every tenant-owned row carries ``tenant_id``; names are unique per tenant.
- References between tenant-owned tables are composite ``(tenant_id, x_id)`` foreign keys, so a
  reference cannot cross tenants even if row-level security were bypassed.
- Child tables (ports, routes, members) copy ``tenant_id`` so RLS policies stay one-line checks.
- Enumerations are ``text`` with CHECK constraints, which are easy to evolve in migrations.

The RLS policies themselves are created in the migration; see
``switchyard/db/migrations/versions/0001_initial.py``.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CIDR, INET, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from switchyard.db.base import (
    Base,
    EntityMixin,
    TenantOwnedMixin,
    tenant_fk,
    tenant_unique,
)
from switchyard.domain import models as dm


def _check(column: str, values: type[enum.StrEnum]) -> CheckConstraint:
    allowed = ", ".join(f"'{m.value}'" for m in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


class TenantRow(EntityMixin, Base):
    """``tenant``: root of ownership."""

    __tablename__ = "tenant"
    __table_args__ = (UniqueConstraint("name"),)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    oidc_group: Mapped[str | None] = mapped_column(Text)
    quotas: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class NetworkRow(TenantOwnedMixin, Base):
    """``network``."""

    __tablename__ = "network"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        CheckConstraint("version >= 1", name="version"),
    )

    cidr: Mapped[str] = mapped_column(CIDR, nullable=False)
    gateway: Mapped[str | None] = mapped_column(INET)
    sdn_zone: Mapped[str | None] = mapped_column(Text)
    vnet: Mapped[str | None] = mapped_column(Text)
    vlan_or_vni: Mapped[int | None] = mapped_column(Integer)
    managed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class VipPoolRow(TenantOwnedMixin, Base):
    """``vip_pool``: address ranges stored as a JSON array of ``{start, end}``."""

    __tablename__ = "vip_pool"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("network_id", "network"),
        CheckConstraint("version >= 1", name="version"),
    )

    network_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ranges: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)


class SeGroupRow(TenantOwnedMixin, Base):
    """``se_group``."""

    __tablename__ = "se_group"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        _check("ha_mode", dm.HaMode),
        CheckConstraint("version >= 1", name="version"),
    )

    ha_mode: Mapped[str] = mapped_column(Text, nullable=False)
    maxconn: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("20000"))


class SeGroupNetworkRow(Base):
    """``se_group_network``: networks attached to an SE group, with their role."""

    __tablename__ = "se_group_network"
    __table_args__ = (
        PrimaryKeyConstraint("se_group_id", "network_id", "role"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("se_group_id", "se_group", cascade=True),
        tenant_fk("network_id", "network"),
        _check("role", dm.NetworkRole),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    se_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    network_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class HealthMonitorRow(TenantOwnedMixin, Base):
    """``health_monitor``."""

    __tablename__ = "health_monitor"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        _check("type", dm.MonitorType),
        CheckConstraint("version >= 1", name="version"),
    )

    type: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int | None] = mapped_column(Integer)
    interval_s: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False)
    rise: Mapped[int] = mapped_column(Integer, nullable=False)
    fall: Mapped[int] = mapped_column(Integer, nullable=False)
    http: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class CertificateRow(TenantOwnedMixin, Base):
    """``certificate``: metadata only; ``secret_ref`` points into OpenBao."""

    __tablename__ = "certificate"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        _check("source", dm.CertSource),
        _check("renew_policy", dm.RenewPolicy),
        CheckConstraint("version >= 1", name="version"),
    )

    source: Mapped[str] = mapped_column(Text, nullable=False)
    sans: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    not_after: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    renew_policy: Mapped[str] = mapped_column(Text, nullable=False)


class PolicySetRow(TenantOwnedMixin, Base):
    """``policy_set``: ``redirect`` rules (JSON) or an ``allow_list`` of CIDRs."""

    __tablename__ = "policy_set"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        CheckConstraint("kind IN ('redirect', 'allow_list')", name="kind"),
        CheckConstraint(
            "(kind = 'redirect' AND rules IS NOT NULL AND cidrs IS NULL)"
            " OR (kind = 'allow_list' AND cidrs IS NOT NULL AND rules IS NULL)",
            name="payload_matches_kind",
        ),
        CheckConstraint("managed_by IS NULL OR managed_by = 'cloudflare'", name="managed_by"),
        CheckConstraint("version >= 1", name="version"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    cidrs: Mapped[list[str] | None] = mapped_column(ARRAY(CIDR))
    managed_by: Mapped[str | None] = mapped_column(Text)


class PoolRow(TenantOwnedMixin, Base):
    """``pool``."""

    __tablename__ = "pool"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("health_monitor_id", "health_monitor"),
        _check("algorithm", dm.Algorithm),
        CheckConstraint("version >= 1", name="version"),
    )

    algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    persistence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    health_monitor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tls: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    tls_verify: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class PoolMemberRow(Base):
    """``pool_member``."""

    __tablename__ = "pool_member"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("pool_id", "pool", cascade=True),
        UniqueConstraint("pool_id", "position"),
        _check("admin_state", dm.AdminState),
        CheckConstraint("weight BETWEEN 0 AND 256", name="weight"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    pool_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str] = mapped_column(INET, nullable=False)
    port: Mapped[int | None] = mapped_column(Integer)
    weight: Mapped[int] = mapped_column(Integer, nullable=False)
    admin_state: Mapped[str] = mapped_column(Text, nullable=False)


class VirtualServiceRow(TenantOwnedMixin, Base):
    """``virtual_service`` (VIP included, as ``vs_vip`` would be one-to-one in M1)."""

    __tablename__ = "virtual_service"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("se_group_id", "se_group"),
        tenant_fk("vip_pool_id", "vip_pool"),
        tenant_fk("default_pool_id", "pool"),
        tenant_fk("allow_list_id", "policy_set"),
        _check("protocol", dm.Protocol),
        CheckConstraint("version >= 1", name="version"),
    )

    se_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    protocol: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    vip: Mapped[str] = mapped_column(INET, nullable=False)
    vip_pool_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    default_pool_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    allow_list_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    enforce_allow_list: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    restore_client_ip_header: Mapped[str | None] = mapped_column(Text)


class VsPortRow(Base):
    """``vs_port``."""

    __tablename__ = "vs_port"
    __table_args__ = (
        PrimaryKeyConstraint("vs_id", "port"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("vs_id", "virtual_service", cascade=True),
        CheckConstraint("port BETWEEN 1 AND 65535", name="port"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    vs_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    tls: Mapped[bool] = mapped_column(Boolean, nullable=False)
    http2: Mapped[bool] = mapped_column(Boolean, nullable=False)
    proxy_protocol: Mapped[bool] = mapped_column(Boolean, nullable=False)
    https_redirect_port: Mapped[int | None] = mapped_column(Integer)


class VsRouteRow(Base):
    """``vs_route``: ``position`` keeps list order, ``route_order`` is the model's ``order``."""

    __tablename__ = "vs_route"
    __table_args__ = (
        PrimaryKeyConstraint("vs_id", "position"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("vs_id", "virtual_service", cascade=True),
        tenant_fk("pool_id", "pool"),
        CheckConstraint(
            "(pool_id IS NULL) <> (redirect_location IS NULL)", name="pool_xor_redirect"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    vs_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    route_order: Mapped[int] = mapped_column(Integer, nullable=False)
    host: Mapped[str | None] = mapped_column(Text)
    path_prefix: Mapped[str | None] = mapped_column(Text)
    path_exact: Mapped[str | None] = mapped_column(Text)
    pool_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    redirect_location: Mapped[str | None] = mapped_column(Text)
    redirect_code: Mapped[int | None] = mapped_column(Integer)


class VsCertificateRow(Base):
    """``vs_certificate``: ordered certificate list of a service (first is the default)."""

    __tablename__ = "vs_certificate"
    __table_args__ = (
        PrimaryKeyConstraint("vs_id", "position"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("vs_id", "virtual_service", cascade=True),
        tenant_fk("certificate_id", "certificate"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    vs_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    certificate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class VsPolicySetRow(Base):
    """``vs_policy_set``: ordered redirect policy sets attached to a service."""

    __tablename__ = "vs_policy_set"
    __table_args__ = (
        PrimaryKeyConstraint("vs_id", "position"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("vs_id", "virtual_service", cascade=True),
        tenant_fk("policy_set_id", "policy_set"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    vs_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


#: Every table that carries ``tenant_id`` and therefore needs an RLS policy.
TENANT_SCOPED_TABLES: tuple[str, ...] = tuple(
    sorted(t.name for t in Base.metadata.sorted_tables if "tenant_id" in t.c)
)
#: Tables whose rows are tenants themselves (policy keyed on ``id``).
TENANT_ROOT_TABLES: tuple[str, ...] = ("tenant",)

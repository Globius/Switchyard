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

from switchyard.db import (
    acme_models,  # noqa: F401  (registers the ACME and DNS provider tables)
    alert_models,  # noqa: F401  (registers the alerting tables)
    auth_models,  # noqa: F401  (registers the login tables)
    gslb_models,  # noqa: F401  (registers the GSLB tables)
    k8s_models,  # noqa: F401  (registers the Kubernetes cluster table)
    ops_models,  # noqa: F401  (registers the M2 tables on the metadata)
)
from switchyard.db.base import (
    Base,
    EntityMixin,
    TenantOwnedMixin,
    enum_check,
    tenant_fk,
    tenant_unique,
)
from switchyard.domain import models as dm


class TenantRow(EntityMixin, Base):
    """``tenant``: root of ownership."""

    __tablename__ = "tenant"
    __table_args__ = (UniqueConstraint("name"),)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    quotas: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    require_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
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


class RoutingProfileRow(TenantOwnedMixin, Base):
    """``routing_profile``: ``kind`` plus its settings as JSON (validated by the domain models)."""

    __tablename__ = "routing_profile"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        CheckConstraint("kind IN ('vrrp', 'bgp')", name="kind"),
        CheckConstraint("version >= 1", name="version"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SeGroupRow(TenantOwnedMixin, Base):
    """``se_group``."""

    __tablename__ = "se_group"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("routing_profile_id", "routing_profile"),
        enum_check("ha_mode", dm.HaMode),
        CheckConstraint("version >= 1", name="version"),
    )

    ha_mode: Mapped[str] = mapped_column(Text, nullable=False)
    routing_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    maxconn: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("20000"))
    canary_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    peers: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class SeGroupNetworkRow(Base):
    """``se_group_network``: networks attached to an SE group, with their role."""

    __tablename__ = "se_group_network"
    __table_args__ = (
        PrimaryKeyConstraint("se_group_id", "network_id", "role"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("se_group_id", "se_group", cascade=True),
        tenant_fk("network_id", "network"),
        enum_check("role", dm.NetworkRole),
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
        enum_check("type", dm.MonitorType),
        CheckConstraint("preset IN ('redis', 'smtp', 'ldap', 'mysql')", name="preset"),
        CheckConstraint("version >= 1", name="version"),
    )

    type: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int | None] = mapped_column(Integer)
    interval_s: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False)
    rise: Mapped[int] = mapped_column(Integer, nullable=False)
    fall: Mapped[int] = mapped_column(Integer, nullable=False)
    http: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    udp: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    preset: Mapped[str | None] = mapped_column(Text)


class CertificateRow(TenantOwnedMixin, Base):
    """``certificate``: metadata only; ``secret_ref`` points into OpenBao."""

    __tablename__ = "certificate"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        enum_check("source", dm.CertSource),
        enum_check("renew_policy", dm.RenewPolicy),
        CheckConstraint("renew_before_days BETWEEN 1 AND 365", name="renew_before_days"),
        CheckConstraint("purpose IN ('server', 'client_ca')", name="purpose"),
        CheckConstraint(
            "challenge IS NULL OR challenge IN ('dns-01', 'http-01', 'tls-alpn-01')",
            name="challenge",
        ),
        CheckConstraint("version >= 1", name="version"),
    )

    source: Mapped[str] = mapped_column(Text, nullable=False)
    sans: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    not_after: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    renew_policy: Mapped[str] = mapped_column(Text, nullable=False)
    renew_before_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30")
    )
    ocsp_stapling: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'server'"))
    crl_secret_ref: Mapped[str | None] = mapped_column(Text)
    acme_provider: Mapped[str | None] = mapped_column(Text)
    challenge: Mapped[str | None] = mapped_column(Text)
    dns_provider: Mapped[str | None] = mapped_column(Text)


class PolicySetRow(TenantOwnedMixin, Base):
    """``policy_set``: rules (JSON; the pages of ``error_pages``) or an ``allow_list`` of CIDRs."""

    __tablename__ = "policy_set"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        CheckConstraint(
            "kind IN ('redirect', 'allow_list', 'rewrite', 'respond', 'error_pages',"
            " 'rate_limit', 'access', 'jwt', 'client_cert', 'directory', 'waf',"
            " 'protection')",
            name="kind",
        ),
        CheckConstraint(
            "(kind NOT IN ('allow_list', 'access', 'client_cert', 'directory')"
            " AND rules IS NOT NULL AND cidrs IS NULL AND config IS NULL)"
            " OR (kind = 'allow_list' AND cidrs IS NOT NULL AND rules IS NULL AND config IS NULL)"
            " OR (kind IN ('access', 'client_cert', 'directory') AND config IS NOT NULL"
            " AND rules IS NULL AND cidrs IS NULL)",
            name="payload_matches_kind",
        ),
        CheckConstraint(
            "managed_by IS NULL OR managed_by IN ('cloudflare', 'geo', 'reputation')",
            name="managed_by",
        ),
        CheckConstraint("version >= 1", name="version"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    cidrs: Mapped[list[str] | None] = mapped_column(ARRAY(CIDR))
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    managed_by: Mapped[str | None] = mapped_column(Text)
    countries: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    feed: Mapped[str | None] = mapped_column(Text)


class PoolRow(TenantOwnedMixin, Base):
    """``pool``."""

    __tablename__ = "pool"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("health_monitor_id", "health_monitor"),
        tenant_fk("client_certificate_id", "certificate"),
        CheckConstraint("send_proxy IN ('v1', 'v2')", name="send_proxy"),
        CheckConstraint("backend_protocol IN ('http1', 'h2', 'h2c')", name="backend_protocol"),
        enum_check("algorithm", dm.Algorithm),
        CheckConstraint("version >= 1", name="version"),
    )

    algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    persistence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    health_monitor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tls: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    tls_verify: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    passive_check: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    slow_start_s: Mapped[int | None] = mapped_column(Integer)
    send_proxy: Mapped[str | None] = mapped_column(Text)
    tunnel_timeout_s: Mapped[int | None] = mapped_column(Integer)
    backend_protocol: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'http1'")
    )
    client_certificate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    discovery: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    labels: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class PoolMemberRow(Base):
    """``pool_member``."""

    __tablename__ = "pool_member"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("pool_id", "pool", cascade=True),
        UniqueConstraint("pool_id", "position"),
        enum_check("admin_state", dm.AdminState),
        CheckConstraint("weight BETWEEN 0 AND 256", name="weight"),
        CheckConstraint("maxconn BETWEEN 1 AND 1000000", name="maxconn"),
        CheckConstraint("drain_timeout_s BETWEEN 1 AND 604800", name="drain_timeout_s"),
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
    backup: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    maxconn: Mapped[int | None] = mapped_column(Integer)
    drain_timeout_s: Mapped[int | None] = mapped_column(Integer)
    drain_since: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


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
        tenant_fk("deny_list_id", "policy_set"),
        enum_check("protocol", dm.Protocol),
        CheckConstraint("version >= 1", name="version"),
    )

    se_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    protocol: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    vip: Mapped[str] = mapped_column(INET, nullable=False)
    vip_pool_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    default_pool_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    allow_list_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deny_list_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    enforce_allow_list: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    restore_client_ip_header: Mapped[str | None] = mapped_column(Text)
    compression: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cache: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    labels: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class VsPortRow(Base):
    """``vs_port``."""

    __tablename__ = "vs_port"
    __table_args__ = (
        PrimaryKeyConstraint("vs_id", "port"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("vs_id", "virtual_service", cascade=True),
        CheckConstraint("port BETWEEN 1 AND 65535", name="port"),
        enum_check("tls_profile", dm.TlsProfile),
        tenant_fk("client_ca_id", "certificate"),
        CheckConstraint("client_verify IN ('required', 'optional')", name="client_verify"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    vs_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    tls: Mapped[bool] = mapped_column(Boolean, nullable=False)
    http2: Mapped[bool] = mapped_column(Boolean, nullable=False)
    proxy_protocol: Mapped[bool] = mapped_column(Boolean, nullable=False)
    https_redirect_port: Mapped[int | None] = mapped_column(Integer)
    tls_profile: Mapped[str | None] = mapped_column(Text)
    client_ca_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    client_verify: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'required'")
    )
    tls_passthrough: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    http3: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class VsRouteRow(Base):
    """``vs_route``: ``position`` keeps list order, ``route_order`` is the model's ``order``."""

    __tablename__ = "vs_route"
    __table_args__ = (
        PrimaryKeyConstraint("vs_id", "position"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        tenant_fk("vs_id", "virtual_service", cascade=True),
        tenant_fk("pool_id", "pool"),
        CheckConstraint(
            "num_nonnulls(pool_id, redirect_location, pool_weights) = 1", name="one_target"
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
    pool_weights: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    fallback_pools: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    match_extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))


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

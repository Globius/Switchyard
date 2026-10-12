"""SQLAlchemy models for change sets, revisions, deployments, audit, jobs, engines and tokens.

These tables belong to milestone M2. They follow the conventions of :mod:`switchyard.db.models`
(ADR 0005). Two of them are append-only by privilege rather than by trigger: ``revision`` and
``audit_event`` grant no ``UPDATE`` or ``DELETE`` to the application role (see the migration).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import BYTEA, INET, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from switchyard.db.base import (
    Base,
    EntityMixin,
    TenantOwnedMixin,
    enum_check,
    tenant_fk,
    tenant_unique,
)
from switchyard.domain.changes import Action, ObjectType
from switchyard.domain.workflow import (
    ChangeSetState,
    DeploymentState,
    EngineState,
    JobState,
    Role,
)


def _now() -> Any:
    return text("now()")


def _timestamp(*, nullable: bool = True, default_now: bool = False) -> Mapped[datetime]:
    return mapped_column(
        TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=_now() if default_now else None,
    )


class ServiceEngineRow(TenantOwnedMixin, Base):
    """``service_engine``: one SE VM and how to reach its Data Plane API."""

    __tablename__ = "service_engine"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        tenant_fk("se_group_id", "se_group"),
        UniqueConstraint("se_group_id", "name"),
        enum_check("state", EngineState),
        CheckConstraint("guest_kind IS NULL OR guest_kind IN ('qemu', 'lxc')", name="guest_kind"),
        CheckConstraint("version >= 1", name="version"),
    )

    se_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mgmt_ip: Mapped[str | None] = mapped_column(INET)
    dataplane_url: Mapped[str] = mapped_column(Text, nullable=False)
    credentials_ref: Mapped[str | None] = mapped_column(Text)
    agent_url: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'enabled'"))
    pve_node: Mapped[str | None] = mapped_column(Text)
    vmid: Mapped[int | None] = mapped_column(Integer)
    guest_kind: Mapped[str | None] = mapped_column(Text)
    data_ips: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    agent_cert_serial: Mapped[str | None] = mapped_column(Text)


class ChangeSetRow(EntityMixin, Base):
    """``change_set``: a draft, pending, running or finished set of operations."""

    __tablename__ = "change_set"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        enum_check("state", ChangeSetState),
        CheckConstraint("version >= 1", name="version"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    message: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    state: Mapped[str] = mapped_column(Text, nullable=False)
    base_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    requested_by: Mapped[str | None] = mapped_column(Text)
    approver: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = _timestamp()
    reject_comment: Mapped[str | None] = mapped_column(Text)
    break_glass_reason: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    finished_at: Mapped[datetime | None] = _timestamp()


class ChangeOpRow(Base):
    """``change_op``: one operation of a change set, in order."""

    __tablename__ = "change_op"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        tenant_fk("change_set_id", "change_set", cascade=True),
        UniqueConstraint("change_set_id", "position"),
        enum_check("object_type", ObjectType),
        enum_check("action", Action),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    change_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expected_version: Mapped[int | None] = mapped_column(Integer)


class RevisionRow(Base):
    """``revision``: an immutable snapshot of a tenant's state after an applied change set."""

    __tablename__ = "revision"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "number"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        CheckConstraint("number >= 1", name="number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    change_set_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    author: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    message: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    git_sha: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class DeploymentRow(Base):
    """``deployment``: the outcome of pushing one revision to one SE group."""

    __tablename__ = "deployment"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        tenant_fk("revision_id", "revision"),
        tenant_fk("se_group_id", "se_group"),
        enum_check("state", DeploymentState),
        Index("ix_deployment_se_group_id_started_at", "se_group_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    change_set_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    se_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    rendered_hash: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    finished_at: Mapped[datetime | None] = _timestamp()


class AuditEventRow(Base):
    """``audit_event``: append-only record of who did what."""

    __tablename__ = "audit_event"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="SET NULL"),
        Index("ix_audit_event_tenant_id_at", "tenant_id", "at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    object_ref: Mapped[str | None] = mapped_column(Text)
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class JobRow(Base):
    """``job``: the PostgreSQL-backed queue (``FOR UPDATE SKIP LOCKED``)."""

    __tablename__ = "job"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        enum_check("state", JobState),
        Index("ix_job_state_run_after", "state", "run_after"),
        Index("ix_job_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    run_after: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = _timestamp()
    heartbeat_at: Mapped[datetime | None] = _timestamp()
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    finished_at: Mapped[datetime | None] = _timestamp()
    started_at: Mapped[datetime | None] = _timestamp()
    change_set_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    stages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    log: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class ApiTokenRow(Base):
    """``api_token``: hashed, scoped, expiring machine credentials."""

    __tablename__ = "api_token"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        UniqueConstraint("token_hash"),
        enum_check("role", Role),
        CheckConstraint(
            "(role = 'system-admin') = (tenant_id IS NULL)", name="system_role_has_no_tenant"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = _timestamp()
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    last_used_at: Mapped[datetime | None] = _timestamp()
    revoked_at: Mapped[datetime | None] = _timestamp()


class EnrollmentTokenRow(Base):
    """``enrollment_token``: one-time (``enroll``) or rotating (``renew``) agent credentials.

    Only the SHA-256 of the token is stored. A token works once: using it sets ``used_at``.
    """

    __tablename__ = "enrollment_token"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "engine_id"],
            ["service_engine.tenant_id", "service_engine.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("token_hash"),
        CheckConstraint("kind IN ('enroll', 'renew')", name="kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    engine_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = _timestamp(nullable=False)
    used_at: Mapped[datetime | None] = _timestamp()
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class EngineStatusRow(Base):
    """``engine_status``: the latest poll result of one engine (one row per engine)."""

    __tablename__ = "engine_status"
    __table_args__ = (ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),)

    engine_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    se_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    reachable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    haproxy_fingerprint: Mapped[str | None] = mapped_column(Text)
    members: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    agent: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    drift: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    observed_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class SdnObjectRow(Base):
    """``sdn_object``: a Proxmox SDN zone or VNet the controller itself created.

    Only these may be deleted once no managed network uses them; objects that already existed in
    Proxmox are never recorded and so never touched.
    """

    __tablename__ = "sdn_object"
    __table_args__ = (ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)  # "zone" | "vnet"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class AutoscaleStateRow(Base):
    """``autoscale_state``: what the autoscaler last saw and did for one SE group (ADR 0194).

    ``decision`` is ``idle``, ``busy`` (over the threshold, waiting out ``sustain_s``),
    ``scaling`` (an engine is deploying or enrolling), ``cooldown``, ``at_max`` or ``blocked``
    (``reason`` says why: an unhealthy group, an engine that never enrolled, a failed deploy).
    """

    __tablename__ = "autoscale_state"
    __table_args__ = (ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),)

    se_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    busy_since: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_scaled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    pending_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    pending_engine_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    enroll_sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    pending_since: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    utilization: Mapped[int | None] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'idle'"))
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class PacketCaptureRow(Base):
    """``packet_capture``: a capture taken on an engine, with its pcap, until ``expires_at``."""

    __tablename__ = "packet_capture"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        CheckConstraint("state IN ('queued', 'running', 'done', 'failed')", name="state"),
        Index("ix_packet_capture_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    engine_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    engine_name: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    packets: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    error: Mapped[str | None] = mapped_column(Text)
    data: Mapped[bytes | None] = mapped_column(BYTEA)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    finished_at: Mapped[datetime | None] = _timestamp()
    expires_at: Mapped[datetime] = _timestamp(nullable=False)

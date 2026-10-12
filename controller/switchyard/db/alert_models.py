"""SQLAlchemy models for alerting (ADR 0120): targets, rules, their state and delivery log.

All four tables are tenant-owned with RLS like the rest. Target ``config`` holds addresses and
URLs only; a signing secret is a ``secret_ref`` into OpenBao (CLAUDE.md: no secrets in PostgreSQL).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from switchyard.db.base import Base, TenantOwnedMixin, tenant_unique


def _timestamp(*, nullable: bool = True, default_now: bool = False) -> Mapped[datetime]:
    return mapped_column(
        TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=text("now()") if default_now else None,
    )


class AlertTargetRow(TenantOwnedMixin, Base):
    """``alert_target``: where a tenant's alerts go (webhook, chat or email)."""

    __tablename__ = "alert_target"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        CheckConstraint("kind IN ('webhook', 'chat', 'email')", name="kind"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class AlertRuleRow(TenantOwnedMixin, Base):
    """``alert_rule``: what to watch (an event, a metric or certificate expiry) and who to tell."""

    __tablename__ = "alert_rule"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        CheckConstraint("kind IN ('event', 'metric', 'cert_expiry')", name="kind"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: Names of the targets in the same tenant.
    targets: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: Event rules read the audit log from here on.
    last_eval_at: Mapped[datetime | None] = _timestamp()
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)


class AlertStateRow(Base):
    """``alert_state``: whether one watched thing (a rule and a key) is firing."""

    __tablename__ = "alert_state"
    __table_args__ = (
        UniqueConstraint("rule_id", "key"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["rule_id"], ["alert_rule.id"], ondelete="CASCADE"),
        CheckConstraint("state IN ('pending', 'firing', 'ok')", name="state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rule_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    since: Mapped[datetime] = _timestamp(nullable=False, default_now=True)
    notified_at: Mapped[datetime | None] = _timestamp()
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class AlertDeliveryRow(Base):
    """``alert_delivery``: one attempt to tell one target, kept for a while (housekeeping)."""

    __tablename__ = "alert_delivery"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        CheckConstraint("status IN ('sent', 'failed')", name="status"),
        Index("ix_alert_delivery_tenant_id_at", "tenant_id", "at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rule: Mapped[str | None] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``firing``, ``resolved`` or ``test``.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = _timestamp(nullable=False, default_now=True)

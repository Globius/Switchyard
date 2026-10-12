# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Kubernetes clusters a tenant connects (D2, ADR 0151).

The row holds how to reach the cluster and what to do with it; the service-account token is in the
secrets backend (``token_ref``). The connector's own state (the change set it is waiting for and
the result of the last sync) lives here too.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKeyConstraint, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from switchyard.db.base import Base, TenantOwnedMixin, tenant_fk, tenant_unique


class KubernetesClusterRow(TenantOwnedMixin, Base):
    """``kubernetes_cluster``: a cluster whose Services and Gateways become virtual services."""

    __tablename__ = "kubernetes_cluster"
    __table_args__ = (
        *tenant_unique(),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        tenant_fk("se_group_id", "se_group"),
        tenant_fk("vip_pool_id", "vip_pool"),
        CheckConstraint("mode IN ('nodeport', 'pods')", name="mode"),
        CheckConstraint("version >= 1", name="version"),
    )

    api_url: Mapped[str] = mapped_column(Text, nullable=False)
    ca_pem: Mapped[str | None] = mapped_column(Text)
    token_ref: Mapped[str] = mapped_column(Text, nullable=False)
    se_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    vip_pool_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'nodeport'"))
    class_name: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'switchyard.io/lb'")
    )
    gateway_class: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'switchyard'")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    pending_change_set: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_sync_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    last_sync: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

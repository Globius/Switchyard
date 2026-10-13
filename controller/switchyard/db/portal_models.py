# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Quota requests a tenant files with the platform (ADR 0199)."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from switchyard.db.base import Base, EntityMixin, enum_check
from switchyard.domain.quota_requests import QuotaRequestState


class QuotaRequestRow(EntityMixin, Base):
    """``quota_request``: a tenant's ask for a higher limit on one resource."""

    __tablename__ = "quota_request"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        enum_check("state", QuotaRequestState),
        CheckConstraint("requested_limit > current_limit", name="raises"),
        CheckConstraint("version >= 1", name="version"),
        Index(
            "uq_quota_request_one_pending",
            "tenant_id",
            "resource",
            unique=True,
            postgresql_where=text("state = 'pending'"),
        ),
        Index("ix_quota_request_state", "state", "requested_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    current_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    granted_limit: Mapped[int | None] = mapped_column(Integer)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)

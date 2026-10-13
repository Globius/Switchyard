# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Engine rolls: replacing every engine of an SE group with one from a new template (ADR 0205)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from switchyard.db.base import Base, EntityMixin


class EngineRollRow(EntityMixin, Base):
    """``engine_roll``: one roll over one SE group; ``state`` is the serialized state machine."""

    __tablename__ = "engine_roll"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        CheckConstraint("status IN ('running', 'paused', 'aborted', 'done')", name="status"),
        CheckConstraint("version >= 1", name="version"),
        Index(
            "uq_engine_roll_one_active",
            "se_group_id",
            unique=True,
            postgresql_where=text("status IN ('running', 'paused')"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    se_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    template_vmid: Mapped[int] = mapped_column(Integer, nullable=False)
    target_version: Mapped[str | None] = mapped_column(Text)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    jobs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

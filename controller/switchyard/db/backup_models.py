# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Backup destinations and the record of each backup run (ADR 0205).

System-wide, not tenant data: only the admin role touches these tables. Target settings hold
``secret_ref`` values only, never credentials.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from switchyard.db.base import Base, EntityMixin


class BackupTargetRow(EntityMixin, Base):
    """``backup_target``: a named destination (validated :class:`BackupTargetConfig` as JSON)."""

    __tablename__ = "backup_target"
    __table_args__ = (UniqueConstraint("name"), CheckConstraint("version >= 1", name="version"))

    name: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class BackupRunRow(Base):
    """``backup_run``: one attempt to write an archive. Kept after the archive is pruned."""

    __tablename__ = "backup_run"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'failed')", name="status"),
        CheckConstraint("size_bytes >= 0", name="size"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    target_name: Mapped[str] = mapped_column(Text, nullable=False)
    archive_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    sha256: Mapped[str | None] = mapped_column(Text)
    controller_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_revision: Mapped[str | None] = mapped_column(Text)
    tenants: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'manual'"))
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

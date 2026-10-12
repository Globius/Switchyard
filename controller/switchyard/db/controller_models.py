"""The controller's own cluster members (ADR 0051, update of 2026-10-12).

Not tenant data: the table is system-wide and only the admin role touches it.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from switchyard.db.base import Base, EntityMixin, enum_check
from switchyard.domain.controller_members import Platform


class ControllerMemberRow(EntityMixin, Base):
    """``controller_member``: one VM of the controller cluster, on Proxmox or outside it."""

    __tablename__ = "controller_member"
    __table_args__ = (
        UniqueConstraint("name"),
        enum_check("platform", Platform),
        CheckConstraint("version >= 1", name="version"),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str | None] = mapped_column(Text)
    external: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    nofailover: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    patroni_url: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

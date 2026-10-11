"""Engines record whether they are a VM or an LXC container.

- ``service_engine.guest_kind``: ``qemu`` or ``lxc`` for engines the controller deployed, NULL for hand-built ones.
  Existing deployed engines (those with a vmid) were all VMs.

Revision ID: 0020
Revises: 0019
"""

from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("service_engine", sa.Column("guest_kind", sa.Text(), nullable=True))
    op.execute("UPDATE service_engine SET guest_kind = 'qemu' WHERE vmid IS NOT NULL")
    op.create_check_constraint(
        op.f("ck_service_engine_guest_kind"),
        "service_engine",
        "guest_kind IS NULL OR guest_kind IN ('qemu', 'lxc')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_service_engine_guest_kind"), "service_engine", type_="check")
    op.drop_column("service_engine", "guest_kind")

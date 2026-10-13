"""The controller's own cluster members, including the external one (ADR 0051).

Revision ID: 0041
Revises: 0040
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "controller_member",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=True),
        sa.Column("external", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("nofailover", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("patroni_url", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "platform IN ('proxmox', 'esxi', 'other')", name=op.f("ck_controller_member_platform")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_controller_member_version")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_controller_member")),
        sa.UniqueConstraint("name", name=op.f("uq_controller_member_name")),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON controller_member TO switchyard_admin")


def downgrade() -> None:
    op.drop_table("controller_member")

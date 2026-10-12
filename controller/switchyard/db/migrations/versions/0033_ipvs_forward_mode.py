"""IPVS forwarding method of a UDP virtual service: ``virtual_service.forward_mode`` (ADR 0192).

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "virtual_service",
        sa.Column("forward_mode", sa.Text(), nullable=False, server_default=sa.text("'nat'")),
    )
    op.create_check_constraint(
        op.f("ck_virtual_service_forward_mode"),
        "virtual_service",
        "forward_mode IN ('nat', 'dr', 'tunnel')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_virtual_service_forward_mode"), "virtual_service", type_="check")
    op.drop_column("virtual_service", "forward_mode")

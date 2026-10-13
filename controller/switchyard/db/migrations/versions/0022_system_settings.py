# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""System settings: controller-wide switches a system administrator controls.

- ``system_setting`` (key, JSON value): today ``allow_lxc_guests`` (default off, ADR 0076).

Revision ID: 0022
Revises: 0021
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_setting",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_setting")),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON system_setting TO switchyard_admin")


def downgrade() -> None:
    op.drop_table("system_setting")

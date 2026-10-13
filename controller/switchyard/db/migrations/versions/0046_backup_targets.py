# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Backup destinations and backup runs (ADR 0205).

Revision ID: 0046
Revises: 0045
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_target",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_backup_target_version")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_target")),
        sa.UniqueConstraint("name", name=op.f("uq_backup_target_name")),
    )
    op.create_table(
        "backup_run",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("target_name", sa.Text(), nullable=False),
        sa.Column("archive_name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("controller_version", sa.Text(), nullable=False),
        sa.Column("schema_revision", sa.Text(), nullable=True),
        sa.Column("tenants", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reason", sa.Text(), server_default=sa.text("'manual'"), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_backup_run_size")),
        sa.CheckConstraint("status IN ('ok', 'failed')", name=op.f("ck_backup_run_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_run")),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON backup_target TO switchyard_admin")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON backup_run TO switchyard_admin")


def downgrade() -> None:
    op.drop_table("backup_run")
    op.drop_table("backup_target")

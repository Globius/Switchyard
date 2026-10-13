# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Automatic SE scale-out: ``se_group.autoscale`` and ``autoscale_state`` (ADR 0194).

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "se_group", sa.Column("autoscale", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_table(
        "autoscale_state",
        sa.Column("se_group_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("busy_since", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_scaled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("pending_job_id", sa.UUID(), nullable=True),
        sa.Column("pending_engine_id", sa.UUID(), nullable=True),
        sa.Column("enroll_sent_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("pending_since", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("utilization", sa.Integer(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False, server_default=sa.text("'idle'")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_autoscale_state_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("se_group_id", name=op.f("pk_autoscale_state")),
    )
    op.execute("ALTER TABLE autoscale_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE autoscale_state FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON autoscale_state "
        "USING (tenant_id = ANY (app_tenant_ids())) "
        "WITH CHECK (tenant_id = ANY (app_tenant_ids()))"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON autoscale_state "
        "TO switchyard_app, switchyard_admin"
    )


def downgrade() -> None:
    op.drop_table("autoscale_state")
    op.drop_column("se_group", "autoscale")

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Jobs: start time, change set link, per-stage progress, log, and the ``cancelled`` state (ADR 0016).

Revision ID: 0008
Revises: 0007
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_OLD = "state IN ('queued', 'running', 'succeeded', 'failed', 'dead')"
_NEW = "state IN ('queued', 'running', 'succeeded', 'failed', 'dead', 'cancelled')"


def upgrade() -> None:
    op.add_column(
        "job", sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column("job", sa.Column("change_set_id", sa.UUID(), nullable=True))
    op.add_column(
        "job",
        sa.Column(
            "stages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "job",
        sa.Column(
            "log",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE job SET change_set_id = (payload->>'change_set_id')::uuid "
        "WHERE payload ? 'change_set_id'"
    )
    op.execute(
        "UPDATE job SET started_at = locked_at WHERE started_at IS NULL AND locked_at IS NOT NULL"
    )
    op.drop_constraint(op.f("ck_job_state"), "job", type_="check")
    op.create_check_constraint(op.f("ck_job_state"), "job", _NEW)
    op.create_index("ix_job_tenant_created", "job", ["tenant_id", "created_at", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_job_tenant_created", table_name="job")
    op.execute("UPDATE job SET state = 'dead' WHERE state = 'cancelled'")
    op.drop_constraint(op.f("ck_job_state"), "job", type_="check")
    op.create_check_constraint(op.f("ck_job_state"), "job", _OLD)
    for column in ("log", "stages", "change_set_id", "started_at"):
        op.drop_column("job", column)

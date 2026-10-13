"""Quota requests: a tenant asks for a higher limit, a system administrator decides (ADR 0199).

Revision ID: 0040
Revises: 0039
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quota_request",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("current_limit", sa.Integer(), nullable=False),
        sa.Column("requested_limit", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column(
            "requested_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("granted_limit", sa.Integer(), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'approved', 'denied', 'withdrawn')",
            name=op.f("ck_quota_request_state"),
        ),
        sa.CheckConstraint("requested_limit > current_limit", name=op.f("ck_quota_request_raises")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_quota_request_version")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_quota_request_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quota_request")),
    )
    op.create_index(
        "uq_quota_request_one_pending",
        "quota_request",
        ["tenant_id", "resource"],
        unique=True,
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index("ix_quota_request_state", "quota_request", ["state", "requested_at"])
    op.execute("ALTER TABLE quota_request ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE quota_request FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON quota_request "
        "USING (tenant_id = ANY (app_tenant_ids())) "
        "WITH CHECK (tenant_id = ANY (app_tenant_ids()))"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON quota_request TO switchyard_app, switchyard_admin"
    )


def downgrade() -> None:
    op.drop_table("quota_request")

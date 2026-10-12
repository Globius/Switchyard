"""Embedded Grafana: single-use tickets and the proxy sessions they turn into (ADR 0080).

- ``grafana_grant``: SHA-256 of the ticket and of the proxy cookie, the tenant the session is
  scoped to, the browser session it came from, and the Grafana login and organization it maps to.
  System-wide table, read and written with the admin role only, like ``user_session``.

Revision ID: 0023
Revises: 0022
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grafana_grant",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ticket_hash", sa.Text(), nullable=False),
        sa.Column("session_hash", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("user_session_id", sa.UUID(), nullable=True),
        sa.Column("landing", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ticket_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("redeemed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("session_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("grafana_login", sa.Text(), nullable=True),
        sa.Column("grafana_org_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grafana_grant")),
        sa.UniqueConstraint("ticket_hash", name=op.f("uq_grafana_grant_ticket_hash")),
        sa.UniqueConstraint("session_hash", name=op.f("uq_grafana_grant_session_hash")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_grafana_grant_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_session_id"],
            ["user_session.id"],
            name=op.f("fk_grafana_grant_user_session_id_user_session"),
            ondelete="CASCADE",
        ),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON grafana_grant TO switchyard_admin")


def downgrade() -> None:
    op.drop_table("grafana_grant")

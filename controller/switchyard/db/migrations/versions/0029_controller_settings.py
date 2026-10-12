"""Controller settings (ADR 0081) and the tls-alpn-01 challenge (ADR 0082).

- ``controller_settings``: the public URL and the change waiting for confirmation.
- ``certificate.challenge`` may now be ``tls-alpn-01``.

Revision ID: 0029
Revises: 0023
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0029"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "controller_settings",
        sa.Column("id", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("public_url", sa.Text(), nullable=True),
        sa.Column("pending", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_by", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_controller_settings_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_controller_settings")),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON controller_settings TO switchyard_admin")
    op.drop_constraint(op.f("ck_certificate_challenge"), "certificate", type_="check")
    op.create_check_constraint(
        op.f("ck_certificate_challenge"),
        "certificate",
        "challenge IS NULL OR challenge IN ('dns-01', 'http-01', 'tls-alpn-01')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_certificate_challenge"), "certificate", type_="check")
    op.create_check_constraint(
        op.f("ck_certificate_challenge"),
        "certificate",
        "challenge IS NULL OR challenge IN ('dns-01', 'http-01')",
    )
    op.drop_table("controller_settings")

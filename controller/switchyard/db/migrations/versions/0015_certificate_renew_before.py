"""Certificate renewal window: how many days before expiry an ``auto`` certificate is renewed.

- ``certificate.renew_before_days`` (1 to 365, default 30: the value that was hard-coded).

Revision ID: 0015
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "certificate",
        sa.Column("renew_before_days", sa.Integer(), server_default=sa.text("30"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_certificate_renew_before_days"),
        "certificate",
        "renew_before_days BETWEEN 1 AND 365",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_certificate_renew_before_days"), "certificate", type_="check")
    op.drop_column("certificate", "renew_before_days")

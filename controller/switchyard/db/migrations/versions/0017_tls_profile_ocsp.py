"""TLS profiles on ports and OCSP stapling on certificates.

- ``vs_port.tls_profile``: ``modern``, ``intermediate``, ``legacy`` or null (engine default).
- ``certificate.ocsp_stapling``: let the engine fetch and staple OCSP responses.

Revision ID: 0017
Revises: 0016
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vs_port", sa.Column("tls_profile", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_vs_port_tls_profile"),
        "vs_port",
        "tls_profile IN ('modern', 'intermediate', 'legacy')",
    )
    op.add_column(
        "certificate",
        sa.Column("ocsp_stapling", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("certificate", "ocsp_stapling")
    op.drop_constraint(op.f("ck_vs_port_tls_profile"), "vs_port", type_="check")
    op.drop_column("vs_port", "tls_profile")

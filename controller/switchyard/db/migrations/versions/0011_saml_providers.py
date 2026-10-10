"""SAML identity providers: protocol settings, request id on login state, assertion replay cache.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("identity_provider", "issuer_url", existing_type=sa.Text(), nullable=True)
    op.alter_column("identity_provider", "client_id", existing_type=sa.Text(), nullable=True)
    op.add_column(
        "identity_provider",
        sa.Column("saml", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_identity_provider_settings_match_protocol"),
        "identity_provider",
        "(protocol = 'oidc' AND issuer_url IS NOT NULL AND client_id IS NOT NULL) "
        "OR (protocol = 'saml' AND saml IS NOT NULL)",
    )
    op.alter_column("login_state", "nonce", existing_type=sa.Text(), nullable=True)
    op.alter_column("login_state", "code_verifier", existing_type=sa.Text(), nullable=True)
    op.add_column("login_state", sa.Column("request_id", sa.Text(), nullable=True))
    op.create_table(
        "saml_assertion_seen",
        sa.Column("provider_id", sa.UUID(), nullable=False),
        sa.Column("assertion_id", sa.Text(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["identity_provider.id"],
            name=op.f("fk_saml_assertion_seen_provider_id_identity_provider"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("provider_id", "assertion_id", name=op.f("pk_saml_assertion_seen")),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON saml_assertion_seen TO switchyard_admin")


def downgrade() -> None:
    op.drop_table("saml_assertion_seen")
    op.drop_column("login_state", "request_id")
    op.execute("DELETE FROM login_state WHERE nonce IS NULL")
    op.alter_column("login_state", "code_verifier", existing_type=sa.Text(), nullable=False)
    op.alter_column("login_state", "nonce", existing_type=sa.Text(), nullable=False)
    op.drop_constraint(
        op.f("ck_identity_provider_settings_match_protocol"), "identity_provider", type_="check"
    )
    op.execute("DELETE FROM identity_provider WHERE protocol = 'saml'")
    op.drop_column("identity_provider", "saml")
    op.alter_column("identity_provider", "client_id", existing_type=sa.Text(), nullable=False)
    op.alter_column("identity_provider", "issuer_url", existing_type=sa.Text(), nullable=False)

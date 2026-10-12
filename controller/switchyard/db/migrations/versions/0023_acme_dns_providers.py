"""ACME providers, DNS credentials and the issuance settings of a certificate (ADR 0079).

- ``acme_provider``: system-wide certificate authorities (admin role only).
- ``dns_provider``: a tenant's DNS credentials, with row-level security like every tenant table.
- ``certificate.acme_provider``, ``challenge`` and ``dns_provider``: how an ACME certificate is
  issued and renewed.

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
        "acme_provider",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("preset", sa.Text(), server_default=sa.text("'custom'"), nullable=False),
        sa.Column("directory_url", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("auth_mode", sa.Text(), server_default=sa.text("'none'"), nullable=False),
        sa.Column("oauth", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("oauth_secret_ref", sa.Text(), nullable=True),
        sa.Column("eab_kid", sa.Text(), nullable=True),
        sa.Column("eab_hmac_ref", sa.Text(), nullable=True),
        sa.Column("account_key_ref", sa.Text(), nullable=False),
        sa.Column(
            "account_registered", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_acme_provider_version")),
        sa.CheckConstraint(
            "auth_mode IN ('none', 'eab', 'oauth')", name=op.f("ck_acme_provider_auth_mode")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_acme_provider")),
        sa.UniqueConstraint("name", name=op.f("uq_acme_provider_name")),
    )
    op.create_index(
        "uq_acme_provider_one_default",
        "acme_provider",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON acme_provider TO switchyard_admin")

    op.create_table(
        "dns_provider",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "zones",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("secret_ref", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_dns_provider_version")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_dns_provider_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dns_provider")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_dns_provider_tenant_id_name")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_dns_provider_tenant_id_id")),
    )
    op.execute("ALTER TABLE dns_provider ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE dns_provider FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON dns_provider "
        "USING (tenant_id = ANY (app_tenant_ids())) "
        "WITH CHECK (tenant_id = ANY (app_tenant_ids()))"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON dns_provider TO switchyard_app, switchyard_admin"
    )

    op.add_column("certificate", sa.Column("acme_provider", sa.Text(), nullable=True))
    op.add_column("certificate", sa.Column("challenge", sa.Text(), nullable=True))
    op.add_column("certificate", sa.Column("dns_provider", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_certificate_challenge"),
        "certificate",
        "challenge IS NULL OR challenge IN ('dns-01', 'http-01')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_certificate_challenge"), "certificate", type_="check")
    op.drop_column("certificate", "dns_provider")
    op.drop_column("certificate", "challenge")
    op.drop_column("certificate", "acme_provider")
    op.drop_table("dns_provider")
    op.drop_table("acme_provider")

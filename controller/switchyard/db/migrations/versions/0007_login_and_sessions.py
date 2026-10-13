# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Login: identity providers, role mappings, login state, user sessions, break-glass admin.

These are system-wide tables used by the authentication layer before a tenant is known, so they are
granted to the admin role only (the application role never reads them).

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TABLES = ("identity_provider", "idp_role_mapping", "login_state", "user_session", "local_admin")


def _ts(name: str, *, nullable: bool = True, now: bool = False):  # type: ignore[no-untyped-def]
    return sa.Column(
        name,
        postgresql.TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if now else None,
    )


def upgrade() -> None:
    op.create_table(
        "identity_provider",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("protocol", sa.Text(), server_default=sa.text("'oidc'"), nullable=False),
        sa.Column("issuer_url", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_secret_ref", sa.Text(), nullable=True),
        sa.Column(
            "scopes", sa.Text(), server_default=sa.text("'openid profile email'"), nullable=False
        ),
        sa.Column(
            "username_claim",
            sa.Text(),
            server_default=sa.text("'preferred_username'"),
            nullable=False,
        ),
        sa.Column("email_claim", sa.Text(), server_default=sa.text("'email'"), nullable=False),
        sa.Column("groups_claim", sa.Text(), server_default=sa.text("'groups'"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _ts("created_at", nullable=False, now=True),
        sa.CheckConstraint(
            "protocol IN ('oidc', 'saml')", name=op.f("ck_identity_provider_protocol")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_provider")),
        sa.UniqueConstraint("name", name=op.f("uq_identity_provider_name")),
    )
    op.create_table(
        "idp_role_mapping",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider_id", sa.UUID(), nullable=False),
        sa.Column("group_name", sa.Text(), nullable=False),
        sa.Column("scope_tenant_id", sa.UUID(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('viewer', 'operator', 'tenant-admin', 'system-admin')",
            name=op.f("ck_idp_role_mapping_role"),
        ),
        sa.CheckConstraint(
            "(role = 'system-admin') = (scope_tenant_id IS NULL)",
            name=op.f("ck_idp_role_mapping_system_role_has_no_tenant"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["identity_provider.id"],
            name=op.f("fk_idp_role_mapping_provider_id_identity_provider"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scope_tenant_id"],
            ["tenant.id"],
            name=op.f("fk_idp_role_mapping_scope_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idp_role_mapping")),
        sa.UniqueConstraint(
            "provider_id",
            "group_name",
            "scope_tenant_id",
            name=op.f("uq_idp_role_mapping_provider_id_group_name_scope_tenant_id"),
        ),
    )
    op.create_table(
        "login_state",
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.UUID(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("return_to", sa.Text(), server_default=sa.text("'/'"), nullable=False),
        _ts("expires_at", nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["identity_provider.id"],
            name=op.f("fk_login_state_provider_id_identity_provider"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("state", name=op.f("pk_login_state")),
    )
    op.create_table(
        "user_session",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column(
            "grants",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("break_glass", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _ts("created_at", nullable=False, now=True),
        _ts("expires_at", nullable=False),
        _ts("last_seen_at", nullable=False, now=True),
        _ts("revoked_at"),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["identity_provider.id"],
            name=op.f("fk_user_session_provider_id_identity_provider"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_session_token_hash")),
    )
    op.create_table(
        "local_admin",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("totp_secret_ref", sa.Text(), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("locked_until"),
        _ts("created_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("username", name=op.f("pk_local_admin")),
    )
    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO switchyard_admin")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)

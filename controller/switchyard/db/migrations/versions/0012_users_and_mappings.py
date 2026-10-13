# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""G1: user accounts, session grants refresh, role-mapping metadata, drop ``tenant.oidc_group``.

``tenant.oidc_group`` was never read by login and duplicates the role mappings (ADR 0030). Each
non-empty value is copied into the audit log (``tenant.oidc_group_dropped``) before the column goes.

Revision ID: 0012
Revises: 0011
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _ts(name: str, *, nullable: bool = True, now: bool = False):  # type: ignore[no-untyped-def]
    return sa.Column(
        name,
        postgresql.TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if now else None,
    )


def upgrade() -> None:
    op.execute(
        "INSERT INTO audit_event (id, tenant_id, actor, action, object_ref, diff) "
        "SELECT gen_random_uuid(), id, 'system:migration', 'tenant.oidc_group_dropped', "
        "'tenant:' || name, jsonb_build_object('oidc_group', oidc_group) "
        "FROM tenant WHERE oidc_group IS NOT NULL AND oidc_group <> ''"
    )
    op.drop_column("tenant", "oidc_group")

    op.add_column("idp_role_mapping", sa.Column("label", sa.Text(), nullable=True))
    op.add_column(
        "idp_role_mapping",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "idp_role_mapping",
        sa.Column("created_by", sa.Text(), server_default=sa.text("'system:cli'"), nullable=False),
    )
    op.add_column("idp_role_mapping", _ts("created_at", nullable=False, now=True))

    op.create_table(
        "user_account",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider_id", sa.UUID(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column(
            "groups",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        _ts("first_login_at", nullable=False, now=True),
        _ts("last_login_at", nullable=False, now=True),
        _ts("last_seen_at", nullable=False, now=True),
        _ts("disabled_at"),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["identity_provider.id"],
            name=op.f("fk_user_account_provider_id_identity_provider"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_account")),
        sa.UniqueConstraint(
            "provider_id", "subject", name=op.f("uq_user_account_provider_subject")
        ),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON user_account TO switchyard_admin")

    op.add_column("user_session", sa.Column("user_id", sa.UUID(), nullable=True))
    op.add_column(
        "user_session",
        sa.Column(
            "groups",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("user_session", _ts("grants_computed_at", nullable=False, now=True))
    op.create_foreign_key(
        op.f("fk_user_session_user_id_user_account"),
        "user_session",
        "user_account",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_session_user_id", table_name="user_session")
    op.drop_constraint(
        op.f("fk_user_session_user_id_user_account"), "user_session", type_="foreignkey"
    )
    op.drop_column("user_session", "grants_computed_at")
    op.drop_column("user_session", "groups")
    op.drop_column("user_session", "user_id")
    op.drop_table("user_account")
    for column in ("created_at", "created_by", "version", "label"):
        op.drop_column("idp_role_mapping", column)
    op.add_column("tenant", sa.Column("oidc_group", sa.Text(), nullable=True))

"""G1: identity-provider versions and test sign-in, break-glass enrollment and timed enablement.

- ``identity_provider.version`` for ``If-Match``.
- ``login_state.purpose`` / ``test_id`` and the ``idp_test`` table: an administrator's test sign-in
  creates no session; its outcome is kept five minutes and read once.
- ``local_admin.totp_confirmed_at`` / ``last_login_at``: an administrator enrolled through the API
  cannot log in until it proved it holds the TOTP secret. Rows made earlier by ``syctl local-admin``
  count as confirmed.
- ``break_glass_setting``: the one row that says whether break-glass was switched on, and until when.

Revision ID: 0013
Revises: 0012
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
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
    op.add_column(
        "identity_provider",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "login_state",
        sa.Column("purpose", sa.Text(), server_default=sa.text("'login'"), nullable=False),
    )
    op.add_column("login_state", sa.Column("test_id", sa.UUID(), nullable=True))
    op.create_table(
        "idp_test",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _ts("created_at", nullable=False, now=True),
        _ts("expires_at", nullable=False),
        sa.CheckConstraint("status IN ('pending', 'done')", name=op.f("ck_idp_test_status")),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["identity_provider.id"],
            name=op.f("fk_idp_test_provider_id_identity_provider"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idp_test")),
    )
    op.add_column("local_admin", _ts("totp_confirmed_at"))
    op.add_column("local_admin", _ts("last_login_at"))
    op.execute("UPDATE local_admin SET totp_confirmed_at = created_at")
    op.create_table(
        "break_glass_setting",
        sa.Column("id", sa.Integer(), server_default=sa.text("1"), nullable=False),
        _ts("enabled_until"),
        sa.Column("enabled_by", sa.Text(), nullable=True),
        _ts("enabled_at"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_break_glass_setting_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_break_glass_setting")),
    )
    for table in ("idp_test", "break_glass_setting"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO switchyard_admin")


def downgrade() -> None:
    op.drop_table("break_glass_setting")
    op.drop_column("local_admin", "last_login_at")
    op.drop_column("local_admin", "totp_confirmed_at")
    op.drop_table("idp_test")
    op.drop_column("login_state", "test_id")
    op.drop_column("login_state", "purpose")
    op.drop_column("identity_provider", "version")

"""M3: agent enrollment tokens (hashed, single use), with RLS like every tenant table.

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _rls() -> None:
    op.execute("ALTER TABLE enrollment_token ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE enrollment_token FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON enrollment_token "
        "USING (tenant_id = ANY (app_tenant_ids())) "
        "WITH CHECK (tenant_id = ANY (app_tenant_ids()))"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON enrollment_token "
        "TO switchyard_app, switchyard_admin"
    )


def upgrade() -> None:
    op.create_table(
        "enrollment_token",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("engine_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('enroll', 'renew')", name=op.f("ck_enrollment_token_kind")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "engine_id"],
            ["service_engine.tenant_id", "service_engine.id"],
            name=op.f("fk_enrollment_token_tenant_id_engine_id_service_engine"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_enrollment_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_enrollment_token_token_hash")),
    )
    _rls()


def downgrade() -> None:
    op.drop_table("enrollment_token")

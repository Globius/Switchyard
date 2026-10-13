"""Devices and device enrollment tokens (ADR 0196): agent-based device posture.

- ``device_enrollment``: one-time tokens (hashed) that let a device agent enroll against a
  ``posture`` policy set.
- ``device``: an enrolled device, its Ed25519 public key (the report signature), its latest
  posture report and the certificate last issued to it.

Both tables are tenant tables with forced row-level security like every other.

Revision ID: 0036
Revises: 0041
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0036"
down_revision = "0041"
branch_labels = None
depends_on = None


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (tenant_id = ANY (app_tenant_ids())) "
        "WITH CHECK (tenant_id = ANY (app_tenant_ids()))"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO switchyard_app, switchyard_admin"
    )


def upgrade() -> None:
    op.create_table(
        "device_enrollment",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("policy_id", sa.UUID(), nullable=False),
        sa.Column("label", sa.Text(), server_default=sa.text("''"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["tenant_id", "policy_id"],
            ["policy_set.tenant_id", "policy_set.id"],
            name=op.f("fk_device_enrollment_tenant_id_policy_id_policy_set"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_enrollment")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_device_enrollment_token_hash")),
    )
    _rls("device_enrollment")
    op.create_table(
        "device",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("policy_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("compliant", sa.Boolean(), nullable=True),
        sa.Column(
            "failures",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_report_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_ts", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cert_serial", sa.Text(), nullable=True),
        sa.Column("cert_not_after", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "enrolled_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active', 'revoked')", name=op.f("ck_device_status")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "policy_id"],
            ["policy_set.tenant_id", "policy_set.id"],
            name=op.f("fk_device_tenant_id_policy_id_policy_set"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_device_tenant_id_id")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_device_tenant_id_name")),
    )
    _rls("device")


def downgrade() -> None:
    op.drop_table("device")
    op.drop_table("device_enrollment")

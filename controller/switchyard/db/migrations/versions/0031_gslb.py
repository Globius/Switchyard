"""GSLB: zones and wide IPs on PowerDNS, tenant-owned with RLS (ADR 0160).

Revision ID: 0031
Revises: 0030
"""

from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

TABLES = ("gslb_zone", "gslb_wide_ip")


def _ts(name: str, *, nullable: bool = False, now: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        name,
        postgresql.TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if now else None,
    )


def _base() -> list[Any]:
    return [
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "gslb_zone",
        *_base(),
        sa.Column("nameservers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dnssec", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", sa.Text(), nullable=False),
        _ts("created_at", now=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_gslb_zone_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gslb_zone")),
        sa.UniqueConstraint("name", name=op.f("uq_gslb_zone_name")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_gslb_zone_tenant_id_id")),
    )
    op.create_table(
        "gslb_wide_ip",
        *_base(),
        sa.Column("zone_id", sa.UUID(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("applied_hash", sa.Text(), nullable=True),
        sa.Column("measurements", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _ts("applied_at", nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        _ts("created_at", now=True),
        sa.CheckConstraint("version >= 1", name=op.f("ck_gslb_wide_ip_version")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_gslb_wide_ip_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "zone_id"],
            ["gslb_zone.tenant_id", "gslb_zone.id"],
            name=op.f("fk_gslb_wide_ip_tenant_id_zone_id_gslb_zone"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gslb_wide_ip")),
        sa.UniqueConstraint("zone_id", "name", name=op.f("uq_gslb_wide_ip_zone_id_name")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_gslb_wide_ip_tenant_id_id")),
    )
    for table in TABLES:
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


def downgrade() -> None:
    op.drop_table("gslb_wide_ip")
    op.drop_table("gslb_zone")

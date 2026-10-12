"""Kubernetes clusters for the connector (ADR 0151).

Revision ID: 0030
Revises: 0029
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kubernetes_cluster",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("api_url", sa.Text(), nullable=False),
        sa.Column("ca_pem", sa.Text(), nullable=True),
        sa.Column("token_ref", sa.Text(), nullable=False),
        sa.Column("se_group_id", sa.UUID(), nullable=False),
        sa.Column("vip_pool_id", sa.UUID(), nullable=False),
        sa.Column("mode", sa.Text(), server_default=sa.text("'nodeport'"), nullable=False),
        sa.Column(
            "class_name", sa.Text(), server_default=sa.text("'switchyard.io/lb'"), nullable=False
        ),
        sa.Column(
            "gateway_class", sa.Text(), server_default=sa.text("'switchyard'"), nullable=False
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("pending_change_set", sa.UUID(), nullable=True),
        sa.Column("last_sync_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_sync_ok", sa.Boolean(), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("last_sync", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("mode IN ('nodeport', 'pods')", name=op.f("ck_kubernetes_cluster_mode")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_kubernetes_cluster_version")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_kubernetes_cluster_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "se_group_id"],
            ["se_group.tenant_id", "se_group.id"],
            name=op.f("fk_kubernetes_cluster_tenant_id_se_group_id_se_group"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "vip_pool_id"],
            ["vip_pool.tenant_id", "vip_pool.id"],
            name=op.f("fk_kubernetes_cluster_tenant_id_vip_pool_id_vip_pool"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kubernetes_cluster")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_kubernetes_cluster_tenant_id_name")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_kubernetes_cluster_tenant_id_id")),
    )
    op.execute("ALTER TABLE kubernetes_cluster ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE kubernetes_cluster FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON kubernetes_cluster "
        "USING (tenant_id = ANY (app_tenant_ids())) "
        "WITH CHECK (tenant_id = ANY (app_tenant_ids()))"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON kubernetes_cluster "
        "TO switchyard_app, switchyard_admin"
    )


def downgrade() -> None:
    op.drop_table("kubernetes_cluster")

"""Record the Proxmox SDN zones and VNets the controller created, so cleanup is safe (ADR 0017).

Revision ID: 0009
Revises: 0008
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sdn_object",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('zone', 'vnet')", name=op.f("ck_sdn_object_kind")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_sdn_object_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "kind", "name", name=op.f("pk_sdn_object")),
    )
    op.execute("ALTER TABLE sdn_object ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sdn_object FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON sdn_object "
        "USING (tenant_id = ANY (app_tenant_ids())) "
        "WITH CHECK (tenant_id = ANY (app_tenant_ids()))"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON sdn_object TO switchyard_app, switchyard_admin"
    )


def downgrade() -> None:
    op.drop_table("sdn_object")

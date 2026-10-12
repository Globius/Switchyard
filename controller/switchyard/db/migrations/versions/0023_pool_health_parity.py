"""Pool and health parity: member backup and maxconn, PROXY protocol, backend client certificate, check presets.

- ``pool_member.backup`` and ``pool_member.maxconn``.
- ``pool.send_proxy`` (``v1`` or ``v2``) and ``pool.client_certificate_id`` (same-tenant certificate).
- ``health_monitor.preset`` (``redis``, ``smtp``, ``ldap`` or ``mysql``).

Cookie, header and URI persistence and the HTTP check body and header expectations live in the
existing ``pool.persistence`` and ``health_monitor.http`` JSON columns and need no change.

Revision ID: 0023
Revises: 0022
"""

from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pool_member",
        sa.Column("backup", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("pool_member", sa.Column("maxconn", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_pool_member_maxconn"), "pool_member", "maxconn BETWEEN 1 AND 1000000"
    )
    op.add_column("pool", sa.Column("send_proxy", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("ck_pool_send_proxy"), "pool", "send_proxy IN ('v1', 'v2')")
    op.add_column("pool", sa.Column("client_certificate_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_pool_tenant_id_client_certificate_id_certificate"),
        "pool",
        "certificate",
        ["tenant_id", "client_certificate_id"],
        ["tenant_id", "id"],
    )
    op.add_column("health_monitor", sa.Column("preset", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_health_monitor_preset"),
        "health_monitor",
        "preset IN ('redis', 'smtp', 'ldap', 'mysql')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_health_monitor_preset"), "health_monitor", type_="check")
    op.drop_column("health_monitor", "preset")
    op.drop_constraint(
        op.f("fk_pool_tenant_id_client_certificate_id_certificate"), "pool", type_="foreignkey"
    )
    op.drop_column("pool", "client_certificate_id")
    op.drop_constraint(op.f("ck_pool_send_proxy"), "pool", type_="check")
    op.drop_column("pool", "send_proxy")
    op.drop_constraint(op.f("ck_pool_member_maxconn"), "pool_member", type_="check")
    op.drop_column("pool_member", "maxconn")
    op.drop_column("pool_member", "backup")

"""Pool, health and traffic parity (P2 and P4).

Pool and health:

- ``pool_member.backup``, ``pool_member.maxconn``, ``pool_member.drain_timeout_s`` and
  ``pool_member.drain_since`` (when the controller saw the member start draining; runtime state,
  not part of the snapshot).
- ``pool.send_proxy`` (``v1`` or ``v2``) and ``pool.client_certificate_id`` (same-tenant certificate).
- ``health_monitor.preset`` (``redis``, ``smtp``, ``ldap`` or ``mysql``).

Traffic features:

- ``vs_port.tls_passthrough`` and ``vs_port.http3``.
- ``virtual_service.compression`` (JSON).
- ``vs_route.pool_weights`` (JSON list of ``{pool_id, weight}``); a route has exactly one of ``pool_id``,
  ``redirect_location`` and ``pool_weights``.
- ``virtual_service.cache`` (JSON) and ``vs_route.fallback_pools`` (JSON list of pool ids).
- ``se_group.peers`` (JSON, see ``PeerSync``).
- ``pool.backend_protocol`` (``http1``, ``h2`` or ``h2c``) and ``pool.tunnel_timeout_s``.

Cookie, header and URI persistence and the HTTP check body and header expectations live in the
existing ``pool.persistence`` and ``health_monitor.http`` JSON columns and need no change.

Revision ID: 0028
Revises: 0027
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pool_member",
        sa.Column("backup", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("pool_member", sa.Column("maxconn", sa.Integer(), nullable=True))
    op.add_column("pool_member", sa.Column("drain_timeout_s", sa.Integer(), nullable=True))
    op.add_column(
        "pool_member", sa.Column("drain_since", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_pool_member_drain_timeout_s"),
        "pool_member",
        "drain_timeout_s BETWEEN 1 AND 604800",
    )
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
    for column in ("tls_passthrough", "http3"):
        op.add_column(
            "vs_port",
            sa.Column(column, sa.Boolean(), server_default=sa.text("false"), nullable=False),
        )
    op.add_column("virtual_service", sa.Column("compression", postgresql.JSONB(), nullable=True))
    op.add_column("vs_route", sa.Column("pool_weights", postgresql.JSONB(), nullable=True))
    op.drop_constraint(op.f("ck_vs_route_pool_xor_redirect"), "vs_route", type_="check")
    op.create_check_constraint(
        op.f("ck_vs_route_one_target"),
        "vs_route",
        "num_nonnulls(pool_id, redirect_location, pool_weights) = 1",
    )
    op.add_column("virtual_service", sa.Column("cache", postgresql.JSONB(), nullable=True))
    op.add_column("vs_route", sa.Column("fallback_pools", postgresql.JSONB(), nullable=True))
    op.add_column("se_group", sa.Column("peers", postgresql.JSONB(), nullable=True))
    op.add_column("pool", sa.Column("tunnel_timeout_s", sa.Integer(), nullable=True))
    op.add_column(
        "pool",
        sa.Column("backend_protocol", sa.Text(), server_default=sa.text("'http1'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_pool_backend_protocol"), "pool", "backend_protocol IN ('http1', 'h2', 'h2c')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_pool_backend_protocol"), "pool", type_="check")
    op.drop_column("pool", "backend_protocol")
    op.drop_column("pool", "tunnel_timeout_s")
    op.drop_column("se_group", "peers")
    op.drop_column("vs_route", "fallback_pools")
    op.drop_column("virtual_service", "cache")
    op.drop_constraint(op.f("ck_vs_route_one_target"), "vs_route", type_="check")
    op.create_check_constraint(
        op.f("ck_vs_route_pool_xor_redirect"),
        "vs_route",
        "(pool_id IS NULL) <> (redirect_location IS NULL)",
    )
    op.drop_column("vs_route", "pool_weights")
    op.drop_column("virtual_service", "compression")
    op.drop_column("vs_port", "http3")
    op.drop_column("vs_port", "tls_passthrough")
    op.drop_constraint(op.f("ck_health_monitor_preset"), "health_monitor", type_="check")
    op.drop_column("health_monitor", "preset")
    op.drop_constraint(
        op.f("fk_pool_tenant_id_client_certificate_id_certificate"), "pool", type_="foreignkey"
    )
    op.drop_column("pool", "client_certificate_id")
    op.drop_constraint(op.f("ck_pool_send_proxy"), "pool", type_="check")
    op.drop_column("pool", "send_proxy")
    op.drop_constraint(op.f("ck_pool_member_maxconn"), "pool_member", type_="check")
    op.drop_constraint(op.f("ck_pool_member_drain_timeout_s"), "pool_member", type_="check")
    op.drop_column("pool_member", "drain_since")
    op.drop_column("pool_member", "drain_timeout_s")
    op.drop_column("pool_member", "maxconn")
    op.drop_column("pool_member", "backup")

"""Alerting (ADR 0120): targets, rules, state and delivery log, all tenant-owned with RLS.

Revision ID: 0025
Revises: 0024
"""

from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

TABLES = ("alert_target", "alert_rule", "alert_state", "alert_delivery")


def _ts(name: str, *, nullable: bool = False, now: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        name,
        postgresql.TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if now else None,
    )


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"],
        ["tenant.id"],
        name=op.f(f"fk_{table}_tenant_id_tenant"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    for table, kinds in (
        ("alert_target", "'webhook', 'chat', 'email'"),
        ("alert_rule", "'event', 'metric', 'cert_expiry'"),
    ):
        columns: list[Any] = [
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("tenant_id", sa.UUID(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
        ]
        if table == "alert_target":
            columns += [
                sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
                sa.Column("secret_ref", sa.Text(), nullable=True),
            ]
        else:
            columns += [
                sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
                sa.Column("targets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            ]
        columns += [
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        ]
        if table == "alert_rule":
            columns.append(_ts("last_eval_at", nullable=True))
        columns += [sa.Column("created_by", sa.Text(), nullable=False), _ts("created_at", now=True)]
        op.create_table(
            table,
            *columns,
            sa.CheckConstraint(f"kind IN ({kinds})", name=op.f(f"ck_{table}_kind")),
            _tenant_fk(table),
            sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
            sa.UniqueConstraint("tenant_id", "name", name=op.f(f"uq_{table}_tenant_id_name")),
            sa.UniqueConstraint("tenant_id", "id", name=op.f(f"uq_{table}_tenant_id_id")),
        )
    op.create_table(
        "alert_state",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("rule_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        _ts("since", now=True),
        _ts("notified_at", nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'firing', 'ok')", name=op.f("ck_alert_state_state")
        ),
        _tenant_fk("alert_state"),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["alert_rule.id"],
            name=op.f("fk_alert_state_rule_id_alert_rule"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_state")),
        sa.UniqueConstraint("rule_id", "key", name=op.f("uq_alert_state_rule_id_key")),
    )
    op.create_table(
        "alert_delivery",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("rule", sa.Text(), nullable=True),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        _ts("at", now=True),
        sa.CheckConstraint("status IN ('sent', 'failed')", name=op.f("ck_alert_delivery_status")),
        _tenant_fk("alert_delivery"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_delivery")),
    )
    op.create_index("ix_alert_delivery_tenant_id_at", "alert_delivery", ["tenant_id", "at"])
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
    for table in reversed(TABLES):
        op.drop_table(table)

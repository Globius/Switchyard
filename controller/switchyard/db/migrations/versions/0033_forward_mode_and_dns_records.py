# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""UDP forwarding method and DNS records published from virtual services (ADR 0192, 0193).

Adds ``virtual_service.forward_mode`` (``nat``, ``dr`` or ``tunnel``), ``virtual_service.dns``
and ``dns_record``, which remembers which records the controller created so it never edits or
deletes one it does not own.

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "virtual_service",
        sa.Column("forward_mode", sa.Text(), nullable=False, server_default=sa.text("'nat'")),
    )
    op.create_check_constraint(
        op.f("ck_virtual_service_forward_mode"),
        "virtual_service",
        "forward_mode IN ('nat', 'dr', 'tunnel')",
    )
    op.add_column(
        "virtual_service", sa.Column("dns", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_table(
        "dns_record",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("virtual_service_id", sa.UUID(), nullable=False),
        sa.Column("hostname", sa.Text(), nullable=False),
        sa.Column("rtype", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("ttl", sa.Integer(), nullable=False),
        sa.Column("proxied", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("handle", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("rtype IN ('A', 'AAAA')", name=op.f("ck_dns_record_rtype")),
        sa.CheckConstraint(
            "state IN ('pending', 'published', 'error')", name=op.f("ck_dns_record_state")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_dns_record_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dns_record")),
        sa.UniqueConstraint(
            "tenant_id", "hostname", "rtype", name=op.f("uq_dns_record_tenant_id_hostname_rtype")
        ),
    )
    op.execute("ALTER TABLE dns_record ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE dns_record FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON dns_record "
        "USING (tenant_id = ANY (app_tenant_ids())) "
        "WITH CHECK (tenant_id = ANY (app_tenant_ids()))"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON dns_record TO switchyard_app, switchyard_admin"
    )


def downgrade() -> None:
    op.drop_table("dns_record")
    op.drop_column("virtual_service", "dns")
    op.drop_constraint(op.f("ck_virtual_service_forward_mode"), "virtual_service", type_="check")
    op.drop_column("virtual_service", "forward_mode")

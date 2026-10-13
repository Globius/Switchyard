# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pool discovery by Proxmox tag; managed geo and reputation IP lists.

- ``pool.discovery`` (JSON, see ``PoolDiscovery``).
- ``policy_set.countries`` and ``policy_set.feed``; ``managed_by`` also allows ``geo`` and
  ``reputation``; ``virtual_service.deny_list_id`` (ADR 0147).

Revision ID: 0026
Revises: 0025
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pool", sa.Column("discovery", postgresql.JSONB(), nullable=True))
    op.add_column("policy_set", sa.Column("countries", postgresql.ARRAY(sa.Text()), nullable=True))
    op.add_column("policy_set", sa.Column("feed", sa.Text(), nullable=True))
    op.drop_constraint(op.f("ck_policy_set_managed_by"), "policy_set", type_="check")
    op.create_check_constraint(
        op.f("ck_policy_set_managed_by"),
        "policy_set",
        "managed_by IS NULL OR managed_by IN ('cloudflare', 'geo', 'reputation')",
    )
    op.add_column("virtual_service", sa.Column("deny_list_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_virtual_service_tenant_id_deny_list_id_policy_set"),
        "virtual_service",
        "policy_set",
        ["tenant_id", "deny_list_id"],
        ["tenant_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_virtual_service_tenant_id_deny_list_id_policy_set"),
        "virtual_service",
        type_="foreignkey",
    )
    op.drop_column("virtual_service", "deny_list_id")
    op.drop_constraint(op.f("ck_policy_set_managed_by"), "policy_set", type_="check")
    op.create_check_constraint(
        op.f("ck_policy_set_managed_by"),
        "policy_set",
        "managed_by IS NULL OR managed_by = 'cloudflare'",
    )
    op.drop_column("policy_set", "feed")
    op.drop_column("policy_set", "countries")
    op.drop_column("pool", "discovery")

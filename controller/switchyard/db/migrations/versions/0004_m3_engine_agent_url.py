# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""M3: where each engine's sy-agent listens (host:port, gRPC over mTLS); routing profiles in ops.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


_TYPES = "'network', 'vip_pool', 'se_group', 'virtual_service', 'pool', 'health_monitor', 'certificate', 'policy_set'"


def upgrade() -> None:
    op.add_column("service_engine", sa.Column("agent_url", sa.Text(), nullable=True))
    op.drop_constraint(op.f("ck_change_op_object_type"), "change_op", type_="check")
    op.create_check_constraint(
        op.f("ck_change_op_object_type"),
        "change_op",
        f"object_type IN ({_TYPES}, 'routing_profile')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_change_op_object_type"), "change_op", type_="check")
    op.create_check_constraint(
        op.f("ck_change_op_object_type"), "change_op", f"object_type IN ({_TYPES})"
    )
    op.drop_column("service_engine", "agent_url")

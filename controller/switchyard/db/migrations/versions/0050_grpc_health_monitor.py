# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``health_monitor.grpc`` and a ``grpc`` monitor type (ADR 0227).

Revision ID: 0050
Revises: 0049
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

_OLD = "type IN ('tcp', 'http', 'udp')"
_NEW = "type IN ('tcp', 'http', 'udp', 'grpc')"


def _swap(check: str) -> None:
    op.drop_constraint(op.f("ck_health_monitor_type"), "health_monitor", type_="check")
    op.create_check_constraint(op.f("ck_health_monitor_type"), "health_monitor", check)


def upgrade() -> None:
    op.add_column(
        "health_monitor",
        sa.Column("grpc", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    _swap(_NEW)


def downgrade() -> None:
    op.execute(
        "DELETE FROM pool WHERE health_monitor_id IN "
        "(SELECT id FROM health_monitor WHERE type = 'grpc')"
    )
    op.execute("DELETE FROM health_monitor WHERE type = 'grpc'")
    op.drop_column("health_monitor", "grpc")
    _swap(_OLD)

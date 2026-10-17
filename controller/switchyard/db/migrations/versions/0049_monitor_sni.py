# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``health_monitor.sni``: server name a health check presents to a TLS pool (ADR 0222).

Revision ID: 0049
Revises: 0048
"""

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("health_monitor", sa.Column("sni", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("health_monitor", "sni")

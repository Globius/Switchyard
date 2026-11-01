# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``vs_route.client_ip``: per-host client-IP restoration override (ADR 0230).

Existing rows get NULL (inherit the service settings), so nothing changes for them.

Revision ID: 0052
Revises: 0051
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vs_route", sa.Column("client_ip", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("vs_route", "client_ip")

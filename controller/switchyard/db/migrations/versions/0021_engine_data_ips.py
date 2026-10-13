# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Engines remember the addresses the deploy job gave their data NICs.

- ``service_engine.data_ips``: a list of ``{"network_id": ..., "address": ...}``, one entry per addressed
  VIP or backend NIC, so the next deploy does not hand the same address out twice.

Revision ID: 0021
Revises: 0020
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_engine",
        sa.Column(
            "data_ips",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("service_engine", "data_ips")

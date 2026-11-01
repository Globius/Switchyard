# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``virtual_service.restore_client_ip_from_any_source``: explicit opt-in (ADR 0229).

Existing rows get ``false``, so nothing changes for them.

Revision ID: 0051
Revises: 0050
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "virtual_service",
        sa.Column(
            "restore_client_ip_from_any_source",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("virtual_service", "restore_client_ip_from_any_source")

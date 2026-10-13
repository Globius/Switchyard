# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Let the admin role read the schema revision, for the upgrade check (ADR 0205).

Revision ID: 0045
Revises: 0037
"""

from alembic import op

revision = "0045"
# The chain on main is 0041 -> 0036 -> 0037; 0042 to 0044 are reserved elsewhere: re-point when they land.
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON alembic_version TO switchyard_admin")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON alembic_version FROM switchyard_admin")

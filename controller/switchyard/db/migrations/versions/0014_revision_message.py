# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Revision message: free text given when a change set is applied.

- ``change_set.message`` is set by ``POST .../apply``; the pipeline copies it to
  ``revision.message`` and the Git mirror puts it in the commit body.

Revision ID: 0014
Revises: 0013
"""

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("change_set", "revision"):
        op.add_column(
            table, sa.Column("message", sa.Text(), server_default=sa.text("''"), nullable=False)
        )


def downgrade() -> None:
    for table in ("revision", "change_set"):
        op.drop_column(table, "message")

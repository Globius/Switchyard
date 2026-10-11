"""Pool passive checks and slow start.

- ``pool.passive_check`` (JSON, see ``PassiveCheck``) and ``pool.slow_start_s`` (seconds).

Revision ID: 0016
Revises: 0015
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pool", sa.Column("passive_check", postgresql.JSONB(), nullable=True))
    op.add_column("pool", sa.Column("slow_start_s", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("pool", "slow_start_s")
    op.drop_column("pool", "passive_check")

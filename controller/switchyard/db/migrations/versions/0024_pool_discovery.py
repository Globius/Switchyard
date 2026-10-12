"""Pool discovery by Proxmox tag.

- ``pool.discovery`` (JSON, see ``PoolDiscovery``).

Revision ID: 0024
Revises: 0023
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pool", sa.Column("discovery", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("pool", "discovery")

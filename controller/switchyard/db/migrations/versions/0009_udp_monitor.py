"""UDP health monitors: the ``udp`` type and its probe details (ADR 0020).

Revision ID: 0009
Revises: 0008
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_OLD = "type IN ('tcp', 'http')"
_NEW = "type IN ('tcp', 'http', 'udp')"


def upgrade() -> None:
    op.add_column(
        "health_monitor", sa.Column("udp", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.drop_constraint(op.f("ck_health_monitor_type"), "health_monitor", type_="check")
    op.create_check_constraint(op.f("ck_health_monitor_type"), "health_monitor", _NEW)


def downgrade() -> None:
    op.execute(
        "UPDATE pool SET health_monitor_id = NULL WHERE health_monitor_id IN "
        "(SELECT id FROM health_monitor WHERE type = 'udp')"
    )
    op.execute("DELETE FROM health_monitor WHERE type = 'udp'")
    op.drop_constraint(op.f("ck_health_monitor_type"), "health_monitor", type_="check")
    op.create_check_constraint(op.f("ck_health_monitor_type"), "health_monitor", _OLD)
    op.drop_column("health_monitor", "udp")

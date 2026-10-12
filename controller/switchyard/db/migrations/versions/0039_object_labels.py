"""Labels on virtual services and pools (ADR 0198).

Revision ID: 0039
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039"
down_revision = "0032"
branch_labels = None
depends_on = None

_TABLES = ("virtual_service", "pool")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "labels",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )
        op.create_check_constraint(
            op.f(f"ck_{table}_labels_object"), table, "jsonb_typeof(labels) = 'object'"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(op.f(f"ck_{table}_labels_object"), table, type_="check")
        op.drop_column(table, "labels")

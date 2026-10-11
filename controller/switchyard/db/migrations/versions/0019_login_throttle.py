"""Login throttling and TOTP replay protection.

- ``login_throttle``: failures and the end of the current block, per key (source address, or
  account plus source address).
- ``local_admin.totp_last_step``: the last accepted TOTP time step, so a code works once.

Revision ID: 0019
Revises: 0018
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_throttle",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("blocked_until", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_login_throttle")),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON login_throttle TO switchyard_admin")
    op.add_column("local_admin", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("local_admin", "totp_last_step")
    op.drop_table("login_throttle")

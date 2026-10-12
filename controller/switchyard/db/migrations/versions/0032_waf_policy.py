"""WAF policy sets: ``policy_set.kind`` accepts ``waf``.

The settings of a WAF policy are stored as the single element of ``policy_set.rules``, like the
other policy kinds, so the payload constraint is unchanged.

Revision ID: 0032
Revises: 0028
"""

from alembic import op

revision = "0032"
down_revision = "0028"
branch_labels = None
depends_on = None

_OLD = "kind IN ('redirect', 'allow_list', 'rewrite', 'respond', 'error_pages', 'rate_limit')"
_NEW = (
    "kind IN ('redirect', 'allow_list', 'rewrite', 'respond', 'error_pages', 'rate_limit', 'waf')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_policy_set_kind"), "policy_set", type_="check")
    op.create_check_constraint(op.f("ck_policy_set_kind"), "policy_set", _NEW)


def downgrade() -> None:
    op.execute(
        "DELETE FROM vs_policy_set WHERE policy_set_id IN (SELECT id FROM policy_set WHERE kind = 'waf')"
    )
    op.execute("DELETE FROM policy_set WHERE kind = 'waf'")
    op.drop_constraint(op.f("ck_policy_set_kind"), "policy_set", type_="check")
    op.create_check_constraint(op.f("ck_policy_set_kind"), "policy_set", _OLD)

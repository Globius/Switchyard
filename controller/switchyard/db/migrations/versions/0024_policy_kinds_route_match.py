"""Policy-set kinds rewrite, respond and error_pages; extra route match conditions.

- ``policy_set.kind`` accepts the three new kinds; their rules (the pages of ``error_pages``) are
  JSON in ``rules``, as for ``redirect``.
- ``vs_route.match_extra`` (JSON): methods, headers, query parameters, source networks and SNI of
  the route's match (host and path keep their columns).

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

_OLD_KIND = "kind IN ('redirect', 'allow_list')"
_NEW_KIND = "kind IN ('redirect', 'allow_list', 'rewrite', 'respond', 'error_pages')"
_OLD_PAYLOAD = (
    "(kind = 'redirect' AND rules IS NOT NULL AND cidrs IS NULL) "
    "OR (kind = 'allow_list' AND cidrs IS NOT NULL AND rules IS NULL)"
)
_NEW_PAYLOAD = (
    "(kind <> 'allow_list' AND rules IS NOT NULL AND cidrs IS NULL) "
    "OR (kind = 'allow_list' AND cidrs IS NOT NULL AND rules IS NULL)"
)


def _swap(kind: str, payload: str) -> None:
    op.drop_constraint(op.f("ck_policy_set_kind"), "policy_set", type_="check")
    op.drop_constraint(op.f("ck_policy_set_payload_matches_kind"), "policy_set", type_="check")
    op.create_check_constraint(op.f("ck_policy_set_kind"), "policy_set", kind)
    op.create_check_constraint(op.f("ck_policy_set_payload_matches_kind"), "policy_set", payload)


def upgrade() -> None:
    _swap(_NEW_KIND, _NEW_PAYLOAD)
    op.add_column(
        "vs_route",
        sa.Column("match_extra", postgresql.JSONB(none_as_null=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("vs_route", "match_extra")
    op.execute(
        "DELETE FROM vs_policy_set WHERE policy_set_id IN (SELECT id FROM policy_set WHERE kind NOT IN ('redirect', 'allow_list'))"
    )
    op.execute("DELETE FROM policy_set WHERE kind NOT IN ('redirect', 'allow_list')")
    _swap(_OLD_KIND, _OLD_PAYLOAD)

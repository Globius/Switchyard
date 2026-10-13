# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Policy-set kinds rewrite, respond and error_pages; extra route match conditions.

- ``policy_set.kind`` accepts the new kinds (rewrite, respond, error_pages, rate_limit, access);
  ``policy_set.config`` (JSON) holds the settings of access, client_cert and directory sets; their rules (the pages of ``error_pages``) are
  JSON in ``rules``, as for ``redirect``.
- ``vs_route.match_extra`` (JSON): methods, headers, query parameters, source networks and SNI of
  the route's match (host and path keep their columns).

Revision ID: 0027
Revises: 0026
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_OLD_KIND = "kind IN ('redirect', 'allow_list')"
_NEW_KIND = (
    "kind IN ('redirect', 'allow_list', 'rewrite', 'respond', 'error_pages', 'rate_limit', "
    "'access', 'jwt', 'client_cert', 'directory')"
)
_OLD_PAYLOAD = (
    "(kind = 'redirect' AND rules IS NOT NULL AND cidrs IS NULL) "
    "OR (kind = 'allow_list' AND cidrs IS NOT NULL AND rules IS NULL)"
)
_NEW_PAYLOAD = (
    "(kind NOT IN ('allow_list', 'access', 'client_cert', 'directory') "
    "AND rules IS NOT NULL AND cidrs IS NULL AND config IS NULL) "
    "OR (kind = 'allow_list' AND cidrs IS NOT NULL AND rules IS NULL AND config IS NULL) "
    "OR (kind IN ('access', 'client_cert', 'directory') AND config IS NOT NULL AND rules IS NULL "
    "AND cidrs IS NULL)"
)


def _swap(kind: str, payload: str) -> None:
    op.drop_constraint(op.f("ck_policy_set_kind"), "policy_set", type_="check")
    op.drop_constraint(op.f("ck_policy_set_payload_matches_kind"), "policy_set", type_="check")
    op.create_check_constraint(op.f("ck_policy_set_kind"), "policy_set", kind)
    op.create_check_constraint(op.f("ck_policy_set_payload_matches_kind"), "policy_set", payload)


def upgrade() -> None:
    op.add_column(
        "policy_set", sa.Column("config", postgresql.JSONB(none_as_null=True), nullable=True)
    )
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
    op.drop_column("policy_set", "config")

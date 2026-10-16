# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``policy_set.kind`` accepts ``crowdsec`` (ADR 0215); its settings are the single JSON element of ``rules``.

Revision ID: 0048
Revises: 0047
"""

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

_KINDS = (
    "'redirect', 'allow_list', 'rewrite', 'respond', 'error_pages', 'rate_limit',"
    " 'access', 'jwt', 'client_cert', 'directory', 'waf', 'protection', 'posture'"
)
_OLD = f"kind IN ({_KINDS})"
_NEW = f"kind IN ({_KINDS}, 'crowdsec')"


def _swap(check: str) -> None:
    op.drop_constraint(op.f("ck_policy_set_kind"), "policy_set", type_="check")
    op.create_check_constraint(op.f("ck_policy_set_kind"), "policy_set", check)


def upgrade() -> None:
    _swap(_NEW)


def downgrade() -> None:
    op.execute(
        "DELETE FROM vs_policy_set WHERE policy_set_id IN (SELECT id FROM policy_set WHERE kind = 'crowdsec')"
    )
    op.execute("DELETE FROM policy_set WHERE kind = 'crowdsec'")
    _swap(_OLD)

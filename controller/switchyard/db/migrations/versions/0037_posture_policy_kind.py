"""``policy_set.kind`` accepts ``posture`` (ADR 0196); its settings are JSON in ``config``.

Revision ID: 0037
Revises: 0036
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_KINDS = (
    "'redirect', 'allow_list', 'rewrite', 'respond', 'error_pages', 'rate_limit',"
    " 'access', 'jwt', 'client_cert', 'directory', 'waf', 'protection'"
)
_OLD_KIND = f"kind IN ({_KINDS})"
_NEW_KIND = f"kind IN ({_KINDS}, 'posture')"
_OLD_PAYLOAD = (
    "(kind NOT IN ('allow_list', 'access', 'client_cert', 'directory') "
    "AND rules IS NOT NULL AND cidrs IS NULL AND config IS NULL) "
    "OR (kind = 'allow_list' AND cidrs IS NOT NULL AND rules IS NULL AND config IS NULL) "
    "OR (kind IN ('access', 'client_cert', 'directory') AND config IS NOT NULL AND rules IS NULL "
    "AND cidrs IS NULL)"
)
_NEW_PAYLOAD = (
    "(kind NOT IN ('allow_list', 'access', 'client_cert', 'directory', 'posture') "
    "AND rules IS NOT NULL AND cidrs IS NULL AND config IS NULL) "
    "OR (kind = 'allow_list' AND cidrs IS NOT NULL AND rules IS NULL AND config IS NULL) "
    "OR (kind IN ('access', 'client_cert', 'directory', 'posture') AND config IS NOT NULL "
    "AND rules IS NULL AND cidrs IS NULL)"
)


def _swap(kind: str, payload: str) -> None:
    op.drop_constraint(op.f("ck_policy_set_kind"), "policy_set", type_="check")
    op.drop_constraint(op.f("ck_policy_set_payload_matches_kind"), "policy_set", type_="check")
    op.create_check_constraint(op.f("ck_policy_set_kind"), "policy_set", kind)
    op.create_check_constraint(op.f("ck_policy_set_payload_matches_kind"), "policy_set", payload)


def upgrade() -> None:
    _swap(_NEW_KIND, _NEW_PAYLOAD)


def downgrade() -> None:
    op.execute("DELETE FROM policy_set WHERE kind = 'posture'")
    _swap(_OLD_KIND, _OLD_PAYLOAD)

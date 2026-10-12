"""Files for JWT validation: the public keys HAProxy verifies bearer tokens with (ADR 0135).

HAProxy's ``jwt_verify`` reads a key from a file, so each key of a ``jwt`` policy set becomes
``certs/jwt-<set>-<rule>-<key>.pem`` in the agent bundle (public keys only; the agent keeps the
``certs`` directory owner-only and reloads HAProxy when it changes). The functions are pure.
"""

from uuid import UUID

from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot
from switchyard.render import escape as esc

KEY_PATH = "certs/jwt-{set}-{rule}-{key}.pem"
KEY_DIR = "/etc/switchyard/certs"


def key_file(policy_name: str, rule: int, key: int) -> str:
    """Path of a key on the engine, as ``jwt_verify`` names it.

    Example:
        >>> key_file("api-tokens", 1, 2)
        '/etc/switchyard/certs/jwt-api-tokens-1-2.pem'
    """
    return f"{KEY_DIR}/jwt-{esc.ident(policy_name)}-{rule}-{key}.pem"


def jwt_key_files(snapshot: Snapshot, se_group_id: UUID) -> dict[str, str]:
    """The ``certs/jwt-*.pem`` files of the JWT sets the group's enabled HTTP services attach."""
    view = snapshot.for_se_group(se_group_id)
    policies = {p.id: p for p in view.policy_sets}
    files: dict[str, str] = {}
    for svc in view.virtual_services:
        if not svc.enabled or svc.protocol is not dm.Protocol.HTTP:
            continue
        for pid in svc.policy_set_ids:
            policy = policies.get(pid)
            if isinstance(policy, dm.JwtPolicySet):
                for i, rule in enumerate(policy.rules, start=1):
                    for j, key in enumerate(rule.keys, start=1):
                        name = esc.ident(policy.name)
                        files[KEY_PATH.format(set=name, rule=i, key=j)] = key.public_key_pem
    return dict(sorted(files.items()))

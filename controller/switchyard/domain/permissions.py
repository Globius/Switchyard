# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The permission matrix: which role may do what (ADR 0030; design doc 02, "Auth and RBAC").

Roles nest: each one can do everything the weaker ones can. A permission is therefore a
``(verb, object)`` pair with the *minimum* role that holds it, and ``role_in(tenant) >= min_role``
is the whole check. The table below is the specification in code. Services call
:func:`min_role` (through ``switchyard.services.changesets.require``), ``GET /permissions`` serves
:data:`MATRIX`, and a unit test compares it row by row with the table in the design document, so
the three cannot drift apart.

Example:
    >>> min_role("approve", "change_set").value
    'tenant-admin'
    >>> allows(Role.OPERATOR, "apply", "change_set")
    True
    >>> allows(Role.OPERATOR, "manage", "engine")
    False
"""

from dataclasses import dataclass

from switchyard.domain.workflow import Role

#: Roles from weakest to strongest. A role holds every permission of the roles before it.
ROLES: tuple[Role, ...] = (Role.VIEWER, Role.OPERATOR, Role.TENANT_ADMIN, Role.SYSTEM_ADMIN)
ROLE_RANK: dict[Role, int] = {role: rank for rank, role in enumerate(ROLES)}


@dataclass(frozen=True)
class Permission:
    """One cell of the matrix.

    Attributes:
        verb: What is done (``read``, ``write``, ``apply``, ``manage`` ...).
        object: What it is done to (``tenant_data``, ``change_set``, ``engine`` ...).
        min_role: The weakest role that may do it.
        row: Number (from 1) of the row in the matrix table of ``02-backend-design.md`` this
            entry belongs to; one row can cover several entries.
        description: One sentence for tooltips and the permissions reference.
    """

    verb: str
    object: str
    min_role: Role
    row: int
    description: str

    @property
    def group(self) -> str:
        """The permission group the matrix screen shows this entry under (eight groups).

        Example:
            >>> min_perm = next(p for p in MATRIX if p.verb == "rollback")
            >>> min_perm.group
            'Change sets'
        """
        return GROUP_OF_ROW[self.row]


def _p(row: int, verb: str, obj: str, role: Role, description: str) -> Permission:
    return Permission(verb, obj, role, row, description)


#: The eight groups of the role matrix screen, by row of the matrix table in 02.
GROUP_OF_ROW: dict[int, str] = {
    1: "Read",
    2: "Applications",
    3: "Infrastructure",
    **dict.fromkeys(range(4, 10), "Change sets"),
    **dict.fromkeys(range(10, 13), "Runtime"),
    13: "Infrastructure",
    14: "Tenant settings",
    15: "Tenant settings",
    **dict.fromkeys(range(16, 20), "Access"),
    20: "System",
}

V, OP, T, S = Role.VIEWER, Role.OPERATOR, Role.TENANT_ADMIN, Role.SYSTEM_ADMIN

#: The matrix. Row numbers refer to the table in ``02-backend-design.md`` ("Roles and the
#: permission matrix"). ``job_own`` is the job's own action (an operator may retry or cancel the
#: apply of their own change set but not a deployment); ``job`` is any job.
MATRIX: tuple[Permission, ...] = (
    _p(
        1,
        "read",
        "tenant_data",
        V,
        "Read everything in the tenant: objects, drafts, rendered config, revisions, jobs, audit.",
    ),
    _p(
        2,
        "write",
        "application",
        OP,
        "Change virtual services, pools, health monitors, policy sets and certificate records.",
    ),
    _p(
        3,
        "write",
        "infrastructure",
        T,
        "Change networks, VIP pools, SE groups and routing profiles.",
    ),
    _p(4, "edit", "draft", OP, "Open a draft change set, add or remove operations, discard it."),
    _p(
        5,
        "request_approval",
        "change_set",
        OP,
        "Ask for approval of a draft when the tenant requires it.",
    ),
    _p(6, "approve", "change_set", T, "Approve a change set; never its author or the requester."),
    _p(
        6, "reject", "change_set", T, "Reject a change set with a comment, which reopens the draft."
    ),
    _p(
        7,
        "apply",
        "change_set",
        OP,
        "Apply a change set when the tenant does not require approval.",
    ),
    _p(
        8,
        "apply_bypass",
        "change_set",
        S,
        "Apply a change set without approval, with a recorded reason (break glass).",
    ),
    _p(9, "rollback", "revision", T, "Open a change set that restores an earlier revision."),
    _p(10, "set_state", "member", OP, "Enable, disable or drain a pool member at runtime."),
    _p(11, "manage", "certificate", OP, "Import, issue and renew certificates."),
    _p(
        12,
        "retry",
        "job_own",
        OP,
        "Retry a job started by an operator action (applying a change set, certificates).",
    ),
    _p(12, "cancel", "job_own", OP, "Cancel a job started by an operator action."),
    _p(12, "retry", "job", T, "Retry any job, including deployments, SDN and retirement."),
    _p(12, "cancel", "job", T, "Cancel any job, including deployments, SDN and retirement."),
    _p(13, "manage", "engine", T, "Register, enable, disable, retire or remove service engines."),
    _p(13, "create", "enrollment_token", T, "Create an agent enrollment token for an engine."),
    _p(13, "deploy", "se_group", T, "Deploy or resize an SE group on Proxmox."),
    _p(13, "apply", "sdn", T, "Apply the SDN configuration."),
    _p(
        13,
        "manage",
        "kubernetes_cluster",
        T,
        "Connect, change, test and remove the tenant's Kubernetes clusters.",
    ),
    _p(
        13,
        "manage",
        "dns_provider",
        T,
        "Add, change, test and remove the tenant's DNS credentials.",
    ),
    _p(14, "enable", "require_approval", T, "Turn the tenant's require-approval setting on."),
    _p(15, "disable", "require_approval", S, "Turn the tenant's require-approval setting off."),
    _p(14, "request", "quota_request", T, "Ask the platform for a higher quota for your tenant."),
    _p(15, "change", "quota", S, "Change a tenant's quotas."),
    _p(15, "decide", "quota_request", S, "Approve or deny a tenant's request for a higher quota."),
    _p(
        16,
        "manage",
        "token",
        T,
        "List, create and revoke tenant API tokens (never above your own role).",
    ),
    _p(
        17,
        "manage",
        "role_mapping",
        T,
        "Manage the viewer and operator role mappings of your own tenant.",
    ),
    _p(
        18,
        "manage",
        "role_mapping_privileged",
        S,
        "Manage tenant-admin and system-admin mappings, and any other tenant's.",
    ),
    _p(19, "list", "tenant_user", T, "List the people with a grant in your tenant."),
    _p(20, "manage", "tenant", S, "Create and delete tenants."),
    _p(20, "manage", "identity_provider", S, "Manage identity providers and test sign-in."),
    _p(20, "manage", "controller", S, "Change the controller's address, VIP and certificate."),
    _p(20, "manage", "acme_provider", S, "Manage ACME certificate authorities and their accounts."),
    _p(20, "manage", "user", S, "List users, disable or delete them and end their sessions."),
    _p(20, "manage", "system_token", S, "Manage system API tokens."),
    _p(20, "manage", "break_glass", S, "Enroll the break-glass administrator and switch it on."),
)

_INDEX: dict[tuple[str, str], Permission] = {(p.verb, p.object): p for p in MATRIX}
if len(_INDEX) != len(MATRIX):  # pragma: no cover - guards edits of the table above
    raise RuntimeError("duplicate (verb, object) in the permission matrix")


def min_role(verb: str, obj: str) -> Role:
    """The weakest role that holds ``(verb, obj)``.

    Raises:
        KeyError: If the pair is not in :data:`MATRIX` (a programming error, not a user error).
    """
    return _INDEX[(verb, obj)].min_role


def allows(role: Role, verb: str, obj: str) -> bool:
    """True if ``role`` holds ``(verb, obj)``."""
    return ROLE_RANK[role] >= ROLE_RANK[min_role(verb, obj)]


def stronger_or_equal(role: Role, other: Role) -> bool:
    """True if ``role`` is at least as strong as ``other``."""
    return ROLE_RANK[role] >= ROLE_RANK[other]


__all__ = [
    "GROUP_OF_ROW",
    "MATRIX",
    "ROLES",
    "ROLE_RANK",
    "Permission",
    "allows",
    "min_role",
    "stronger_or_equal",
]

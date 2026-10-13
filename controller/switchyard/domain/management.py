# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rules for the locked ``management`` tenant, which hosts the controller itself.

The controller runs on VMs that this platform manages, and its own VIP comes from the management
tenant's SE group (02 "HA and deployment"). A bad change there could cut the controller off from
the platform that would repair it, so:

* every apply in the tenant needs a system administrator and a recorded reason (the existing
  break-glass path, see :func:`is_locked`), and
* the pipeline refuses any change that would remove or disable the virtual service named
  :data:`CONTROLLER_SERVICE` or delete the SE group that serves it (:func:`protected_problems`).

Example:
    >>> is_locked("management"), is_locked("acme")
    (True, False)
"""

from collections.abc import Sequence

from switchyard.domain.changes import Action, ChangeOp, ObjectType
from switchyard.domain.errors import Problem
from switchyard.domain.snapshot import Snapshot

MANAGEMENT_TENANT = "management"
CONTROLLER_SERVICE = "controller"


def is_locked(tenant_name: str) -> bool:
    """Return whether changes to the tenant need a system administrator and a reason."""
    return tenant_name == MANAGEMENT_TENANT


def protected_problems(base: Snapshot, ops: Sequence[ChangeOp]) -> list[Problem]:
    """Findings for operations that would remove or disable the controller's VIP or its SE group.

    Args:
        base: The tenant's committed state (names are looked up here).
        ops: The change set's operations.

    Example:
        With a virtual service named ``controller`` in ``base``, deleting it, deleting its SE group,
        or updating it with ``enabled: false`` each yield one ``controller_protected`` problem.
    """
    service = next((s for s in base.virtual_services if s.name == CONTROLLER_SERVICE), None)
    if service is None:
        return []
    found: list[Problem] = []
    for op in ops:
        if op.object_type is ObjectType.VIRTUAL_SERVICE and op.object_id == service.id:
            disables = op.action is Action.UPDATE and (op.body or {}).get("enabled") is False
            if op.action is Action.DELETE or disables:
                found.append(_problem(f"virtual_services.{service.name}", "the controller VIP"))
        elif (
            op.object_type is ObjectType.SE_GROUP
            and op.object_id == service.se_group_id
            and op.action is Action.DELETE
        ):
            found.append(_problem("se_groups", "the SE group that serves the controller VIP"))
    return found


def _problem(path: str, what: str) -> Problem:
    return Problem(
        path, "controller_protected", f"this change would remove or disable {what}; do it by hand"
    )

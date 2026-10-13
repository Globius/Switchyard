"""Quota requests: how a tenant asks the platform for a higher limit (ADR 0199).

Only system administrators change quotas (``change quota``). A tenant administrator who runs into
one files a *quota request* for the resource, the limit they want and why. A system administrator
approves it (optionally granting less) or denies it with a note. This module holds the rules that
need no database; the service applies them.

Example:
    >>> check_request("virtual_service", 20, 50, "Two new product teams")
    []
    >>> check_request("virtual_service", 20, 10, "x")
    [('requested_limit', 'the requested limit must be higher than the current limit (20)')]
"""

import enum

#: Longest reason or decision note, in characters.
MAX_TEXT = 500
#: Highest limit a request may ask for; a sanity bound, not a policy.
MAX_LIMIT = 1_000_000


class QuotaRequestState(enum.StrEnum):
    """Where a quota request stands."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"


def check_request(
    resource: str, current: int | None, requested: int, reason: str
) -> list[tuple[str, str]]:
    """Return what is wrong with a new request as ``(field, message)`` pairs (empty when fine).

    Args:
        resource: The quota key, for example ``virtual_service``.
        current: The tenant's limit for ``resource``, or ``None`` when it has none (unlimited).
        requested: The limit the tenant wants.
        reason: Why, in the tenant's words.

    Example:
        >>> check_request("pool", None, 10, "more apps")
        [('resource', 'pool has no quota, so there is nothing to raise')]
    """
    problems: list[tuple[str, str]] = []
    if current is None:
        problems.append(("resource", f"{resource} has no quota, so there is nothing to raise"))
    elif requested <= current:
        problems.append(
            (
                "requested_limit",
                f"the requested limit must be higher than the current limit ({current})",
            )
        )
    if requested > MAX_LIMIT:
        problems.append(("requested_limit", f"the requested limit must be at most {MAX_LIMIT}"))
    if not reason.strip():
        problems.append(("reason", "say why the tenant needs it"))
    if len(reason) > MAX_TEXT:
        problems.append(("reason", f"the reason must be at most {MAX_TEXT} characters"))
    return problems


def check_grant(current: int, requested: int, granted: int) -> list[str]:
    """Return what is wrong with the limit an approver grants.

    A grant is at least the current limit (approving never lowers a quota) and at most what was
    asked for.

    Example:
        >>> check_grant(20, 50, 30)
        []
        >>> check_grant(20, 50, 60)
        ['the granted limit must be between 20 and 50']
    """
    if current <= granted <= requested:
        return []
    return [f"the granted limit must be between {current} and {requested}"]

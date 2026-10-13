# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Managed IP lists: keep country and reputation allow-list policy sets in step with their sources.

``sync_ip_lists`` runs every six hours. For each allow-list policy set with ``managed_by: geo`` or
``reputation`` it fetches the networks (the union of the countries, or the named feed) and, when
they differ, puts them in one change set per tenant opened by ``system:iplists`` and applied like
any other (canary, revision, Git mirror; waits for approval where the tenant requires it). Lists
managed by ``cloudflare`` belong to :mod:`switchyard.services.certificates` and are left alone.

Rules that keep a bad feed from taking a service down (ADR 0147):

- A fetch that fails, or answers with no networks, leaves the list as it is.
- A list that would lose more than half of its networks (and had at least 20) is not replaced;
  a log line says so. A truncated download looks exactly like that.
- At most 100,000 networks per list.
- One iplists change set at a time per tenant.

Example:
    >>> merge_networks([["10.0.0.0/8", "192.0.2.0/24"], ["10.0.0.0/8", "2001:db8::/32"]])
    ['10.0.0.0/8', '192.0.2.0/24', '2001:db8::/32']
"""

import ipaddress
import logging
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select

from switchyard.adapters.iplists import MAX_NETWORKS, IpListError, IpLists
from switchyard.db.models import TenantRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import models as dm
from switchyard.domain.changes import Action, ChangeOp, ObjectType
from switchyard.domain.workflow import ChangeSetState, Role
from switchyard.services.changesets import ChangeSetService
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import PermissionDeniedError

log = logging.getLogger("switchyard.iplists")

SYNC_IP_LISTS_JOB = "sync_ip_lists"
ACTOR = "system:iplists"
_OPEN_STATES = {ChangeSetState.DRAFT, ChangeSetState.PENDING_APPROVAL, ChangeSetState.APPLYING}
_SHRINK_GUARD_MIN = 20


def merge_networks(lists: Iterable[Iterable[str]]) -> list[str]:
    """Union of several lists: deduplicated, IPv4 first, in address order.

    Example:
        >>> merge_networks([["192.0.2.0/24"], ["192.0.2.0/24"]])
        ['192.0.2.0/24']
    """
    nets = {ipaddress.ip_network(n) for part in lists for n in part}
    return [
        str(n) for n in sorted(nets, key=lambda n: (n.version, int(n.network_address), n.prefixlen))
    ]


def shrinks_too_far(old: int, new: int) -> bool:
    """True when ``new`` is under half of ``old`` and ``old`` is big enough for that to matter.

    Example:
        >>> shrinks_too_far(1000, 100), shrinks_too_far(10, 1), shrinks_too_far(1000, 600)
        (True, False, False)
    """
    return old >= _SHRINK_GUARD_MIN and new * 2 < old


class IpListSync:
    """Runs the sync for every tenant."""

    def __init__(self, db: Databases, source: IpLists, change_sets: ChangeSetService) -> None:
        """Create the service.

        Args:
            db: Database handles.
            source: Where lists come from.
            change_sets: Used to open and apply the change sets.
        """
        self._db = db
        self._source = source
        self._change_sets = change_sets

    async def _fetch(self, policy: dm.AllowListPolicySet) -> list[str] | None:
        """The wanted networks, or ``None`` when the list must stay as it is."""
        try:
            if policy.managed_by == "geo":
                if not self._source.has_geo():
                    log.warning("%s: country lists are not configured", policy.name)
                    return None
                parts = [await self._source.country(cc) for cc in policy.countries]
            else:
                if policy.feed not in self._source.feeds():
                    log.warning("%s: feed %s is not configured", policy.name, policy.feed)
                    return None
                parts = [await self._source.feed(policy.feed or "")]
        except IpListError as exc:
            log.error("%s: %s", policy.name, exc)
            return None
        wanted = merge_networks(parts)
        if not wanted:
            log.warning("%s: the source answered with no networks; keeping the list", policy.name)
            return None
        if len(wanted) > MAX_NETWORKS:
            log.error("%s: %d networks is over the limit", policy.name, len(wanted))
            return None
        if shrinks_too_far(len(policy.cidrs), len(wanted)):
            log.warning(
                "%s: would shrink from %d to %d networks; keeping the list",
                policy.name,
                len(policy.cidrs),
                len(wanted),
            )
            return None
        return wanted

    async def run(self) -> dict[str, int]:
        """Sync every tenant. Returns the number of lists changed per tenant name.

        A failure for one tenant is logged and does not stop the others.
        """
        async with admin_session(self._db.admin) as session:
            tenants = list((await session.execute(select(TenantRow.id, TenantRow.name))).all())
        changed: dict[str, int] = {}
        for tenant_id, tenant_name in tenants:
            try:
                count = await self.run_tenant(tenant_id)
            except (PermissionDeniedError, ValueError) as exc:
                log.error("ip list sync for %s failed: %s", tenant_name, exc)
                continue
            if count:
                changed[tenant_name] = count
        return changed

    async def run_tenant(self, tenant_id: UUID) -> int:
        """Sync one tenant's managed lists. Returns the number of lists changed."""
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
        managed = [
            p
            for p in snapshot.policy_sets
            if isinstance(p, dm.AllowListPolicySet) and p.managed_by in {"geo", "reputation"}
        ]
        if not managed:
            return 0
        who = Principal(ACTOR, Role.OPERATOR, frozenset({tenant_id}))
        open_sets = await self._change_sets.list_change_sets(who, tenant_id, states=_OPEN_STATES)
        if any(c.author == ACTOR for c in open_sets):
            return 0
        ops: list[ChangeOp] = []
        names: list[str] = []
        for policy in managed:
            wanted = await self._fetch(policy)
            if wanted is None or wanted == merge_networks([[str(c) for c in policy.cidrs]]):
                continue
            ops.append(
                ChangeOp(
                    object_type=ObjectType.POLICY_SET,
                    object_id=policy.id,
                    action=Action.UPDATE,
                    body={"cidrs": wanted},
                    expected_version=policy.version,
                )
            )
            names.append(policy.name)
        if not ops:
            return 0
        opened = await self._change_sets.open(
            who, tenant_id, f"ip lists: refresh {', '.join(names)}"
        )
        await self._change_sets.add_ops(who, tenant_id, opened.record.id, ops)
        try:
            await self._change_sets.apply(who, tenant_id, opened.record.id)
        except PermissionDeniedError:
            await self._change_sets.request_approval(who, tenant_id, opened.record.id)
            log.warning("ip list change set %s waits for approval", opened.record.id)
        return len(ops)

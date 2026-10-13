# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Keep the keys of JWT rules that name a ``jwks_url`` in step with the issuer (ADR 0153).

``sync_jwks`` runs every five minutes. For each ``jwt`` policy set rule with a ``jwks_url`` it
fetches the document (https only, no redirects, 256 KiB at most, the host passes the same address
policy as DNS servers with ``SWITCHYARD_JWKS_CIDRS`` for private networks), converts it with
:mod:`switchyard.services.jwks` and, when the keys differ, opens one change set per tenant as
``system:jwks``. It is applied like any other change set (canary, revision, Git mirror; it waits
for approval where the tenant requires it).

Rules that keep a bad answer from taking an API down:

- A fetch that fails, or answers with no usable key, leaves the rule's keys as they are.
- A rule with no keys yet (just created) answers 401 until the first fetch has been applied.
- One jwks change set at a time per tenant.

Example:
    >>> same_keys([{"alg": "RS256", "public_key_pem": "p", "kid": "a"}],
    ...           [{"kid": "a", "alg": "RS256", "public_key_pem": "p"}])
    True
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy import select

from switchyard.adapters.dns.base import check_endpoint
from switchyard.db.models import TenantRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import models as dm
from switchyard.domain.changes import Action, ChangeOp, ObjectType
from switchyard.domain.workflow import ChangeSetState, Role
from switchyard.services.changesets import ChangeSetService
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import PermissionDeniedError
from switchyard.services.jwks import JwksError, jwks_to_keys

log = logging.getLogger("switchyard.jwks")

SYNC_JWKS_JOB = "sync_jwks"
ACTOR = "system:jwks"
MAX_BYTES = 256 * 1024
_OPEN_STATES = {ChangeSetState.DRAFT, ChangeSetState.PENDING_APPROVAL, ChangeSetState.APPLYING}

Fetch = Callable[[str], Awaitable[str]]


async def http_fetch(url: str) -> str:
    """Download a JWKS document.

    Raises:
        JwksError: If the address is not allowed, the answer is not ``200`` or is too long.
    """
    host = urlparse(url).hostname or ""
    try:
        check_endpoint(host, "jwks_url", env="SWITCHYARD_JWKS_CIDRS")
    except ValueError as exc:
        raise JwksError(str(exc)) from exc
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise JwksError(f"cannot fetch {url}: {exc}") from exc
    if response.status_code != 200:
        raise JwksError(f"{url} answered HTTP {response.status_code}")
    if len(response.content) > MAX_BYTES:
        raise JwksError(f"{url} answered with more than {MAX_BYTES // 1024} KiB")
    return response.text


def _norm(keys: Any) -> list[tuple[str, str, str]]:
    return sorted(
        (
            str(k.get("kid") or ""),
            str(k["alg"]),
            str(k["public_key_pem"]).strip(),
        )
        for k in keys
    )


def same_keys(a: Any, b: Any) -> bool:
    """True if two key lists hold the same keys, in any order."""
    return _norm(a) == _norm(b)


class JwksSync:
    """Runs the sync for every tenant."""

    def __init__(
        self, db: Databases, change_sets: ChangeSetService, fetch: Fetch = http_fetch
    ) -> None:
        """Create the service.

        Args:
            db: Database handles.
            change_sets: Used to open and apply the change sets.
            fetch: Downloads a URL (replaced in tests).
        """
        self._db = db
        self._change_sets = change_sets
        self._fetch = fetch

    async def run(self) -> dict[str, int]:
        """Sync every tenant. Returns the number of policy sets changed per tenant name."""
        async with admin_session(self._db.admin) as session:
            tenants = list((await session.execute(select(TenantRow.id, TenantRow.name))).all())
        changed: dict[str, int] = {}
        for tenant_id, tenant_name in tenants:
            try:
                count = await self.run_tenant(tenant_id)
            except (PermissionDeniedError, ValueError) as exc:
                log.error("jwks sync for %s failed: %s", tenant_name, exc)
                continue
            if count:
                changed[tenant_name] = count
        return changed

    async def run_tenant(self, tenant_id: UUID) -> int:
        """Sync one tenant's JWT rules. Returns the number of policy sets changed."""
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
        sets = [
            p
            for p in snapshot.policy_sets
            if isinstance(p, dm.JwtPolicySet) and any(r.jwks_url for r in p.rules)
        ]
        if not sets:
            return 0
        who = Principal(ACTOR, Role.OPERATOR, frozenset({tenant_id}))
        open_sets = await self._change_sets.list_change_sets(who, tenant_id, states=_OPEN_STATES)
        if any(c.author == ACTOR for c in open_sets):
            return 0
        fetched: dict[str, list[dict[str, str]] | None] = {}
        ops: list[ChangeOp] = []
        names: list[str] = []
        for policy in sets:
            rules: list[dict[str, Any]] = []
            changed = False
            for rule in policy.rules:
                body = rule.model_dump(mode="json")
                if rule.jwks_url:
                    keys = await self._keys(rule.jwks_url, fetched)
                    if keys is not None and not same_keys(keys, body["keys"]):
                        body["keys"] = keys
                        changed = True
                rules.append(body)
            if not changed:
                continue
            ops.append(
                ChangeOp(
                    object_type=ObjectType.POLICY_SET,
                    object_id=policy.id,
                    action=Action.UPDATE,
                    body={"rules": rules},
                    expected_version=policy.version,
                )
            )
            names.append(policy.name)
        if not ops:
            return 0
        opened = await self._change_sets.open(who, tenant_id, f"jwks: refresh {', '.join(names)}")
        await self._change_sets.add_ops(who, tenant_id, opened.record.id, ops)
        try:
            await self._change_sets.apply(who, tenant_id, opened.record.id)
        except PermissionDeniedError:
            await self._change_sets.request_approval(who, tenant_id, opened.record.id)
            log.warning("jwks change set %s waits for approval", opened.record.id)
        return len(ops)

    async def _keys(
        self, url: str, cache: dict[str, list[dict[str, str]] | None]
    ) -> list[dict[str, str]] | None:
        if url not in cache:
            try:
                cache[url] = jwks_to_keys(await self._fetch(url))
            except JwksError as exc:
                log.error("%s: %s; keeping the keys", url, exc)
                cache[url] = None
        return cache[url]

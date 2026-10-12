"""The controller's own address, VIP and certificate, changed safely from the UI (ADR 0081).

In production the controller is reached through the ``controller`` virtual service of the locked
``management`` tenant (ADR 0051). Changing its FQDN, VIP or certificate can cut the person off from
the very screen they used, so a change is a transaction with a way back:

1. **Propose** validates the new FQDN, VIP and certificate, then applies them to the ``controller``
   service as an ordinary change set of the management tenant (canary and rollout like any other,
   with a recorded reason). A confirmation window starts when the change set is applied.
2. **Confirm** must arrive on the *new* FQDN, which proves a browser can reach it. It makes the new
   FQDN the public URL and ends the transaction.
3. **Revert** (or the window running out, noticed by the worker, never by the API) puts the previous
   VIP and certificate back with another change set.

Only the VIP and the certificate of that one service change; the revert restores exactly those
fields, so other edits made in the meantime survive. The public URL (login redirects, cookies,
SAML entity ids) changes only on confirm. Where there is no management tenant (quick start,
development) only the public URL can be set.

Example:
    >>> window_seconds(None), window_seconds(5), window_seconds(9999)
    (120, 30, 600)
"""

import ipaddress
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select

from switchyard.db import audit_log
from switchyard.db import changesets as store
from switchyard.db.auth_models import ControllerSettingsRow
from switchyard.db.models import TenantRow
from switchyard.db.ops_models import ChangeSetRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import models as dm
from switchyard.domain import validators as v
from switchyard.domain.changes import Action, ChangeError, ChangeOp, ObjectType
from switchyard.domain.management import CONTROLLER_SERVICE, MANAGEMENT_TENANT
from switchyard.domain.workflow import ChangeSetState, Role
from switchyard.services.changesets import ChangeSetService, ValidationFailedError, require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import (
    ConflictError,
    FieldErrors,
    PermissionDeniedError,
)

log = logging.getLogger("switchyard.controller")

CONTROLLER_TICK_JOB = "controller_tick"
DEFAULT_WINDOW_S = 120
MIN_WINDOW_S = 30
MAX_WINDOW_S = 600
#: A change set that has not finished applying after this long is given up on.
APPLY_GIVE_UP = timedelta(minutes=20)
CACHE_S = 5.0


def window_seconds(requested: int | None) -> int:
    """The confirmation window, clamped.

    Example:
        >>> window_seconds(60)
        60
    """
    if requested is None:
        return DEFAULT_WINDOW_S
    return max(MIN_WINDOW_S, min(MAX_WINDOW_S, requested))


@dataclass(frozen=True)
class CertificateSummary:
    """The certificate a service presents."""

    name: str
    sans: list[str]
    not_after: datetime


@dataclass(frozen=True)
class Listen:
    """Where the controller service answers."""

    vip: str
    ports: list[dict[str, Any]]


@dataclass(frozen=True)
class IdpRedirect:
    """The redirect address an identity provider must allow for a new FQDN."""

    provider: str
    protocol: str
    redirect_url: str


@dataclass(frozen=True)
class ControllerView:
    """What the settings screen shows.

    Attributes:
        managed: The management tenant and its ``controller`` service exist.
        fqdn: The public host name now in effect (from the public URL), if known.
        public_url: The public URL now in effect.
        listen: VIP and ports of the ``controller`` service.
        certificate: The certificate the service presents.
        pending: The change waiting for confirmation, if any.
        last_result: How the previous change ended.
        redirect_urls: What each identity provider must allow for the pending FQDN.
        break_glass: How to recover without the UI.
    """

    managed: bool
    fqdn: str | None
    public_url: str | None
    listen: Listen | None
    certificate: CertificateSummary | None
    pending: dict[str, Any] | None
    last_result: dict[str, Any] | None
    redirect_urls: list[IdpRedirect] = field(default_factory=list)
    break_glass: str = (
        "On a controller node: `syctl controller status` and `syctl controller revert` need only "
        "database access; the break-glass login still works."
    )


class ControllerSettings:
    """Propose, confirm and revert changes to the controller's address, VIP and certificate."""

    def __init__(
        self,
        db: Databases,
        change_sets: ChangeSetService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Create the service.

        Args:
            db: Database access.
            change_sets: The change set service the transaction uses.
            clock: Replaceable clock (tests).
        """
        self._db = db
        self._change_sets = change_sets
        self._clock = clock
        self._cache: tuple[float, str | None, dict[str, Any] | None] | None = None

    # -- reading ----------------------------------------------------------------------------------

    async def _row(self, session: Any, *, lock: bool = False) -> ControllerSettingsRow:
        query = select(ControllerSettingsRow).where(ControllerSettingsRow.id == 1)
        if lock:
            query = query.with_for_update()
        row = (await session.execute(query)).scalar_one_or_none()
        if row is None:
            row = ControllerSettingsRow(id=1)
            session.add(row)
            await session.flush()
        return row  # type: ignore[no-any-return]

    async def _management(self) -> tuple[UUID, dm.VirtualService, Any] | None:
        """The management tenant id, its controller service and its snapshot."""
        async with admin_session(self._db.admin) as session:
            tenant_id = (
                await session.execute(
                    select(TenantRow.id).where(TenantRow.name == MANAGEMENT_TENANT)
                )
            ).scalar_one_or_none()
        if tenant_id is None:
            return None
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
        service = next((s for s in snapshot.virtual_services if s.name == CONTROLLER_SERVICE), None)
        return None if service is None else (tenant_id, service, snapshot)

    async def view(
        self, principal: Principal, *, env_public_url: str | None = None
    ) -> ControllerView:
        """The current state of the settings (system administrators)."""
        require(principal, "manage", "controller")
        async with admin_session(self._db.admin) as session:
            row = await self._row(session)
            public_url, pending, last = row.public_url, row.pending, row.last_result
        public_url = public_url or env_public_url
        management = await self._management()
        listen = certificate = None
        if management is not None:
            _, service, snapshot = management
            listen = Listen(
                str(service.vip),
                [
                    {
                        "port": p.port,
                        "tls": p.tls,
                        "https_redirect_port": p.https_redirect_port,
                    }
                    for p in service.ports
                ],
            )
            certs = {c.id: c for c in snapshot.certificates}
            first = next((certs[i] for i in service.certificate_ids if i in certs), None)
            if first is not None:
                certificate = CertificateSummary(first.name, list(first.sans), first.not_after)
        redirects: list[IdpRedirect] = []
        if pending and pending.get("after", {}).get("fqdn"):
            redirects = await self._redirects(pending["after"]["public_url"])
        return ControllerView(
            managed=management is not None,
            fqdn=urlparse(public_url).hostname if public_url else None,
            public_url=public_url,
            listen=listen,
            certificate=certificate,
            pending=self._pending_view(pending),
            last_result=last,
            redirect_urls=redirects,
        )

    def _pending_view(self, pending: dict[str, Any] | None) -> dict[str, Any] | None:
        if not pending:
            return None
        out = dict(pending)
        expires = out.get("expires_at")
        if expires:
            left = datetime.fromisoformat(expires) - self._clock()
            out["seconds_left"] = max(0, int(left.total_seconds()))
        else:
            out["seconds_left"] = None
        return out

    async def redirects_for(self, public_url: str) -> list[IdpRedirect]:
        """What each identity provider must allow so sign-in works at ``public_url``."""
        return await self._redirects(public_url)

    async def _redirects(self, public_url: str) -> list[IdpRedirect]:
        from switchyard.db.auth_models import IdentityProviderRow

        async with admin_session(self._db.admin) as session:
            rows = (
                (
                    await session.execute(
                        select(IdentityProviderRow).order_by(IdentityProviderRow.name)
                    )
                )
                .scalars()
                .all()
            )
        base = public_url.rstrip("/")
        out = []
        for r in rows:
            suffix = f"/api/v1/auth/callback/{r.name if r.protocol == 'oidc' else r.id}"
            out.append(IdpRedirect(r.name, r.protocol, base + suffix))
        return out

    async def public_url(self, host: str | None, env_default: str | None) -> str | None:
        """The public URL for a request that arrived for ``host``.

        The confirmed URL, or the environment's; while a change waits for confirmation the pending
        URL is also used for requests that arrive on the pending host, so signing in works there.
        Read at most every few seconds.
        """
        now = time.monotonic()
        if self._cache is None or now - self._cache[0] > CACHE_S:
            async with admin_session(self._db.admin) as session:
                row = await self._row(session)
                self._cache = (now, row.public_url, row.pending)
        _, stored, pending = self._cache
        if pending and pending.get("state") == "waiting" and host:
            after = pending.get("after", {})
            if after.get("fqdn") and host.split(":")[0].lower() == after["fqdn"]:
                return str(after["public_url"])
        return stored or env_default

    def _forget(self) -> None:
        self._cache = None

    # -- proposing --------------------------------------------------------------------------------

    async def propose(
        self,
        principal: Principal,
        *,
        fqdn: str,
        vip: str | None = None,
        certificate: str | None = None,
        confirm_within_s: int | None = None,
        request_id: str | None = None,
    ) -> ControllerView:
        """Validate and start a change.

        Without a management tenant only ``fqdn`` is accepted: it is the public URL and takes
        effect at confirm.

        Raises:
            FieldErrors: With the path of every problem.
            ConflictError: If a change is already waiting.
        """
        require(principal, "manage", "controller")
        errors = FieldErrors()
        try:
            host = v.validate_hostname(fqdn.strip().lower())
        except ValueError as exc:
            errors.add("fqdn", "invalid", str(exc))
            host = ""
        management = await self._management()
        if management is None and (vip or certificate):
            errors.add(
                "vip" if vip else "certificate",
                "unmanaged",
                "there is no management tenant with a controller service here",
            )
        window = window_seconds(confirm_within_s)
        errors.raise_if_any()
        async with admin_session(self._db.admin) as session:
            row = await self._row(session, lock=True)
            if row.pending:
                raise ConflictError("a change is waiting for confirmation: confirm or revert it")
            current_url = row.public_url
        now = self._clock()
        before_host = urlparse(current_url).hostname if current_url else None
        after: dict[str, Any] = {"fqdn": host}
        before: dict[str, Any] = {"fqdn": before_host}
        ops: list[ChangeOp] = []
        scheme, port_suffix = "https", ""
        tenant_id: UUID | None = None
        if management is not None:
            tenant_id, service, snapshot = management
            scheme, port_suffix = self._origin_of(service)
            ops = self._operations(
                service, snapshot, host, vip, certificate, now, errors, before, after
            )
            errors.raise_if_any()
        after["public_url"] = f"{scheme}://{host}{port_suffix}"
        before["public_url"] = current_url
        pending: dict[str, Any] = {
            "state": "applying" if ops else "waiting",
            "proposed_by": principal.actor,
            "proposed_at": now.isoformat(),
            "window_s": window,
            "expires_at": None if ops else (now + timedelta(seconds=window)).isoformat(),
            "before": before,
            "after": after,
            "change_set_id": None,
            "revision_before": None,
            "revision_after": None,
        }
        if ops and tenant_id is not None:
            await self._apply(principal, tenant_id, ops, pending, host, request_id)
        async with admin_session(self._db.admin) as session:
            row = await self._row(session, lock=True)
            row.pending = pending
            row.updated_by = principal.actor
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="controller.change_proposed",
                object_ref="controller",
                diff={"before": before, "after": after, "window_s": window},
                request_id=request_id,
            )
        self._forget()
        return await self.view(principal)

    @staticmethod
    def _origin_of(service: dm.VirtualService) -> tuple[str, str]:
        tls = [p for p in service.ports if p.tls]
        if tls:
            port = tls[0].port
            return "https", "" if port == 443 else f":{port}"
        port = service.ports[0].port if service.ports else 80
        return "http", "" if port == 80 else f":{port}"

    def _operations(
        self,
        service: dm.VirtualService,
        snapshot: Any,
        host: str,
        vip: str | None,
        certificate: str | None,
        now: datetime,
        errors: FieldErrors,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> list[ChangeOp]:
        body: dict[str, Any] = {}
        before["vip"] = after["vip"] = str(service.vip)
        certs = {c.id: c for c in snapshot.certificates}
        current = next((certs[i] for i in service.certificate_ids if i in certs), None)
        before["certificate"] = after["certificate"] = current.name if current else None
        if vip:
            try:
                address = ipaddress.ip_address(vip)
            except ValueError:
                errors.add("vip", "invalid", "must be an IP address")
            else:
                if address != service.vip:
                    body["vip"] = str(address)
                    after["vip"] = str(address)
        chosen = current
        if certificate:
            found = next((c for c in snapshot.certificates if c.name == certificate), None)
            if found is None:
                errors.add("certificate", "not_found", f"no certificate named {certificate}")
            else:
                chosen = found
                if not any(p.tls for p in service.ports):
                    errors.add(
                        "certificate",
                        "no_tls_port",
                        "the controller service has no TLS port: add port 443 with TLS in the "
                        "management tenant first",
                    )
                if found.id not in service.certificate_ids:
                    body["certificate_ids"] = [str(found.id)]
                after["certificate"] = found.name
        if chosen is not None and any(p.tls for p in service.ports):
            self._check_certificate(chosen, host, now, errors)
        if not body:
            return []
        return [
            ChangeOp(
                object_type=ObjectType.VIRTUAL_SERVICE,
                object_id=service.id,
                action=Action.UPDATE,
                body=body,
                expected_version=service.version,
            )
        ]

    @staticmethod
    def _check_certificate(
        cert: dm.Certificate, host: str, now: datetime, errors: FieldErrors
    ) -> None:
        if cert.purpose != "server":
            errors.add("certificate", "invalid", "not a server certificate")
        if cert.not_after - now < timedelta(days=1):
            errors.add("certificate", "expired", f"{cert.name} expires within a day")
        if not any(v.host_covered_by_san(host, san) for san in cert.sans):
            errors.add("fqdn", "not_covered", f"no name of {cert.name} covers {host}")

    async def _apply(
        self,
        principal: Principal,
        tenant_id: UUID,
        ops: list[ChangeOp],
        pending: dict[str, Any],
        host: str,
        request_id: str | None,
    ) -> None:
        opened = await self._change_sets.open(
            principal, tenant_id, f"controller settings: {host}", request_id=request_id
        )
        change_set_id = opened.record.id
        try:
            await self._change_sets.add_ops(principal, tenant_id, change_set_id, ops)
            async with tenant_session(self._db.app, [tenant_id]) as session:
                pending["revision_before"] = await store.current_revision(session, tenant_id)
            pending["change_set_id"] = str(change_set_id)
            await self._change_sets.apply(
                principal,
                tenant_id,
                change_set_id,
                break_glass_reason=f"controller settings: {pending['proposed_by']} changes {host}",
                request_id=request_id,
            )
        except (ChangeError, ValidationFailedError) as exc:
            await self._discard(principal, tenant_id, change_set_id)
            raise _field_errors(exc) from exc

    async def _discard(self, principal: Principal, tenant_id: UUID, change_set_id: UUID) -> None:
        try:
            await self._change_sets.discard(principal, tenant_id, change_set_id)
        except Exception:
            log.warning("could not discard change set %s", change_set_id)

    # -- confirming and reverting -----------------------------------------------------------------

    async def confirm(
        self, principal: Principal, *, request_host: str | None, request_id: str | None = None
    ) -> ControllerView:
        """Keep the new settings. The request must have arrived on the new FQDN.

        Raises:
            ConflictError: If nothing waits for confirmation.
            PermissionDeniedError: If the request did not come in on the new FQDN.
        """
        require(principal, "manage", "controller")
        async with admin_session(self._db.admin) as session:
            row = await self._row(session, lock=True)
            pending = row.pending
            if not pending or pending.get("state") != "waiting":
                raise ConflictError("no change is waiting for confirmation")
            after = pending["after"]
            changed = after["fqdn"] != pending["before"].get("fqdn")
            seen = (request_host or "").split(":")[0].lower()
            if changed and seen != after["fqdn"]:
                raise PermissionDeniedError(
                    f"confirm from https://{after['fqdn']}: reaching the new address is the proof "
                    "that it works"
                )
            row.public_url = after["public_url"]
            row.last_result = {
                "kind": "confirmed",
                "at": self._clock().isoformat(),
                "by": principal.actor,
                "fqdn": after["fqdn"],
            }
            row.pending = None
            row.updated_by = principal.actor
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="controller.change_confirmed",
                object_ref="controller",
                diff={"after": after},
                request_id=request_id,
            )
        self._forget()
        return await self.view(principal)

    async def revert(
        self, principal: Principal, *, request_id: str | None = None
    ) -> ControllerView:
        """Put the previous VIP and certificate back now."""
        require(principal, "manage", "controller")
        await self._revert(principal.actor, "reverted by an administrator", principal, request_id)
        self._forget()
        return await self.view(principal)

    async def _revert(
        self, actor: str, reason: str, principal: Principal | None, request_id: str | None
    ) -> None:
        async with admin_session(self._db.admin) as session:
            row = await self._row(session, lock=True)
            pending = row.pending
            if not pending:
                raise ConflictError("no change is waiting")
            row.pending = None
            row.updated_by = actor
        undone = await self._undo(pending, actor, principal)
        async with admin_session(self._db.admin) as session:
            row = await self._row(session, lock=True)
            row.last_result = {
                "kind": "reverted",
                "at": self._clock().isoformat(),
                "by": actor,
                "reason": reason,
                "restored": undone,
            }
            await audit_log.record(
                session,
                tenant_id=None,
                actor=actor,
                action="controller.change_reverted",
                object_ref="controller",
                diff={"reason": reason, "restored": undone},
                request_id=request_id,
            )

    async def _undo(self, pending: dict[str, Any], actor: str, principal: Principal | None) -> bool:
        """Restore the VIP and certificate the change replaced. Returns whether anything changed."""
        management = await self._management()
        if management is None or not pending.get("change_set_id"):
            return False
        tenant_id, service, snapshot = management
        before = pending["before"]
        body: dict[str, Any] = {}
        if before.get("vip") and str(service.vip) != before["vip"]:
            body["vip"] = before["vip"]
        if before.get("certificate"):
            wanted = next(
                (c for c in snapshot.certificates if c.name == before["certificate"]), None
            )
            if wanted is not None and service.certificate_ids != (wanted.id,):
                body["certificate_ids"] = [str(wanted.id)]
        if not body:
            return False
        who = principal or Principal(actor, Role.SYSTEM_ADMIN)
        opened = await self._change_sets.open(who, tenant_id, "controller settings: revert")
        op = ChangeOp(
            object_type=ObjectType.VIRTUAL_SERVICE,
            object_id=service.id,
            action=Action.UPDATE,
            body=body,
            expected_version=service.version,
        )
        await self._change_sets.add_ops(who, tenant_id, opened.record.id, [op])
        await self._change_sets.apply(
            who,
            tenant_id,
            opened.record.id,
            break_glass_reason=f"controller settings revert ({actor})",
        )
        return True

    # -- the worker's job -------------------------------------------------------------------------

    async def tick(self) -> list[str]:
        """Advance waiting changes: start the window once applied, give up, or revert at expiry.

        Called by the worker every few seconds; it never needs the API.
        """
        events: list[str] = []
        async with admin_session(self._db.admin) as session:
            pending = (await self._row(session)).pending
        if not pending:
            return events
        now = self._clock()
        if pending["state"] == "applying":
            state, error = await self._change_set_state(pending)
            if state is ChangeSetState.APPLIED:
                window = int(pending["window_s"])
                async with admin_session(self._db.admin) as session:
                    row = await self._row(session, lock=True)
                    if row.pending and row.pending["state"] == "applying":
                        row.pending = {
                            **row.pending,
                            "state": "waiting",
                            "expires_at": (now + timedelta(seconds=window)).isoformat(),
                            "revision_after": await self._current_revision(),
                        }
                events.append("waiting")
            elif state in {ChangeSetState.FAILED, ChangeSetState.DISCARDED} or (
                now - datetime.fromisoformat(pending["proposed_at"]) > APPLY_GIVE_UP
            ):
                await self._fail(pending, error or "the change set did not apply")
                events.append("failed")
        elif pending["state"] == "waiting" and now >= datetime.fromisoformat(pending["expires_at"]):
            await self._revert("system:controller", "not confirmed in time", None, None)
            events.append("expired")
        if events:
            self._forget()
        return events

    async def _change_set_state(
        self, pending: dict[str, Any]
    ) -> tuple[ChangeSetState | None, str | None]:
        if not pending.get("change_set_id"):
            return ChangeSetState.APPLIED, None
        async with admin_session(self._db.admin) as session:
            row = await session.get(ChangeSetRow, UUID(pending["change_set_id"]))
        if row is None:
            return ChangeSetState.DISCARDED, "the change set is gone"
        return ChangeSetState(row.state), row.error

    async def _current_revision(self) -> int | None:
        management = await self._management()
        if management is None:
            return None
        async with tenant_session(self._db.app, [management[0]]) as session:
            return await store.current_revision(session, management[0])

    async def _fail(self, pending: dict[str, Any], error: str) -> None:
        async with admin_session(self._db.admin) as session:
            row = await self._row(session, lock=True)
            row.pending = None
            row.last_result = {
                "kind": "failed",
                "at": self._clock().isoformat(),
                "by": pending.get("proposed_by"),
                "reason": error,
            }
            await audit_log.record(
                session,
                tenant_id=None,
                actor="system:controller",
                action="controller.change_failed",
                object_ref="controller",
                diff={"error": error, "after": pending.get("after")},
            )

    async def status_text(self) -> str:
        """A few lines for the command line."""
        async with admin_session(self._db.admin) as session:
            row = await self._row(session)
            url, pending, last = row.public_url, row.pending, row.last_result
        lines = [f"public URL: {url or '(from SWITCHYARD_PUBLIC_URL)'}"]
        if pending:
            lines.append(
                f"pending: {pending['state']}, {pending['before'].get('fqdn')} -> "
                f"{pending['after'].get('fqdn')}, expires {pending.get('expires_at')}"
            )
        if last:
            lines.append(f"last result: {last.get('kind')} at {last.get('at')}")
        return "\n".join(lines)

    async def reset(self, actor: str = "cli:controller") -> bool:
        """Revert a waiting change without the API (command line, database access only)."""
        async with admin_session(self._db.admin) as session:
            pending = (await self._row(session)).pending
        if not pending:
            return False
        await self._revert(actor, "reverted from the command line", None, None)
        self._forget()
        return True


def _field_errors(exc: ChangeError | ValidationFailedError) -> FieldErrors:
    """Map the findings of the change set onto the fields of the request."""
    errors = FieldErrors()
    if isinstance(exc, ValidationFailedError):
        for problem in exc.problems:
            last = problem.path.rsplit(".", 1)[-1]
            field_name = {"certificate_ids": "certificate", "vip": "vip"}.get(last, "fqdn")
            errors.add(field_name, problem.code, problem.message)
    else:
        errors.add("vip", "invalid", str(exc))
    return errors

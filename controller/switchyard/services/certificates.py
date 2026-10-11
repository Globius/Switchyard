"""Certificate lifecycle and the Cloudflare IP allow list, all through normal change sets.

- ``import_pem``: validate an uploaded bundle, keep the key in the secrets backend, return the
  ``certificate`` object body for the caller's change set.
- ``issue``: create a key and CSR, get a certificate (ACME DNS-01 or Cloudflare Origin CA), keep it
  in the secrets backend, then create or update the ``certificate`` object.
- ``renew_due``: daily; renews automatic certificates 30 days before expiry.
- ``sync_cloudflare_ips``: daily; updates allow-list policy sets with ``managed_by: cloudflare``.

Automated writes are visible, audited change sets like any other (never direct database writes), so
they run through validation, the canary and rollback. When a tenant requires approval the change set
is left waiting for a person.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select

from switchyard.adapters.acme import AcmeError, AcmeIssuer
from switchyard.adapters.cloudflare import Cloudflare, CloudflareError
from switchyard.adapters.secrets import SecretBackendError, SecretsReader, SecretsWriter
from switchyard.db import audit_log
from switchyard.db.models import TenantRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import models as dm
from switchyard.domain.changes import Action, ChangeOp, ObjectType
from switchyard.domain.snapshot import Snapshot
from switchyard.domain.workflow import Role
from switchyard.ids import uuid7
from switchyard.jobs import queue
from switchyard.services.certs import (
    CertificateError,
    CertInfo,
    Pkcs12Error,
    generate_key_and_csr,
    inspect_pem,
    pkcs12_to_pem,
    secret_ref_for,
)
from switchyard.services.changesets import ChangeSetService, require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import FieldErrors, NotFoundError, PermissionDeniedError

log = logging.getLogger("switchyard.certificates")

ISSUE_CERT_JOB = "issue_cert"
RENEW_CERTS_JOB = "renew_certs"
SYNC_CLOUDFLARE_IPS_JOB = "sync_cloudflare_ips"
RENEW_BEFORE = timedelta(days=30)


class SecretStore(SecretsReader, SecretsWriter, Protocol):
    """A secrets backend that can both read and write."""


@dataclass(frozen=True)
class Issued:
    """A certificate that was obtained and stored."""

    info: CertInfo
    secret_ref: str


def _system(tenant_id: UUID) -> Principal:
    return Principal("system:certs", Role.OPERATOR, frozenset({tenant_id}))


class CertificateJobs:
    """Queues certificate jobs (the API side, which holds no issuer credentials)."""

    def __init__(self, db: Databases) -> None:
        """Create the service."""
        self._db = db

    async def request_issue(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        name: str,
        source: dm.CertSource,
        sans: tuple[str, ...],
        request_id: str | None = None,
    ) -> UUID:
        """Queue issuance (or reissue) of a certificate and return the job id."""
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, "manage", "certificate")
        async with admin_session(self._db.admin) as session:
            return await queue.enqueue(
                session,
                ISSUE_CERT_JOB,
                {
                    "tenant_id": str(tenant_id),
                    "name": name,
                    "source": source.value,
                    "sans": list(sans),
                },
                tenant_id=tenant_id,
                max_attempts=2,
                request_id=request_id,
            )

    async def request_renew(
        self,
        principal: Principal,
        tenant_id: UUID,
        certificate_id: UUID,
        *,
        request_id: str | None = None,
    ) -> UUID:
        """Queue a renewal of one existing certificate."""
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, "manage", "certificate")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
        cert = next((c for c in snapshot.certificates if c.id == certificate_id), None)
        if cert is None:
            raise NotFoundError(f"certificate {certificate_id} does not exist")
        return await self.request_issue(
            principal,
            tenant_id,
            name=cert.name,
            source=cert.source,
            sans=cert.sans,
            request_id=request_id,
        )


class CertificateService:
    """Imports, issues and renews certificates for tenants."""

    def __init__(
        self,
        db: Databases,
        secrets: SecretStore,
        change_sets: ChangeSetService,
        *,
        cloudflare: Cloudflare | None = None,
        acme: AcmeIssuer | None = None,
    ) -> None:
        """Create the service; issuers are optional and checked when used."""
        self._db = db
        self._secrets = secrets
        self._change_sets = change_sets
        self._cloudflare = cloudflare
        self._acme = acme

    # -- storing material -------------------------------------------------------------------------

    async def _tenant_name(self, tenant_id: UUID) -> str:
        async with admin_session(self._db.admin) as session:
            name = (
                await session.execute(select(TenantRow.name).where(TenantRow.id == tenant_id))
            ).scalar_one_or_none()
        if name is None:
            raise NotFoundError(f"tenant {tenant_id} does not exist")
        return str(name)

    async def _store(self, tenant_id: UUID, name: str, pem: str) -> Issued:
        info = inspect_pem(pem)
        ref = secret_ref_for(await self._tenant_name(tenant_id), name, info.serial)
        try:
            await self._secrets.put(ref, info.pem)
        except SecretBackendError as exc:
            raise CertificateError(f"could not store the key material: {exc}") from exc
        return Issued(info, ref)

    @staticmethod
    def _body(issued: Issued, source: dm.CertSource, renew: dm.RenewPolicy) -> dict[str, Any]:
        return {
            "source": source.value,
            "sans": list(issued.info.sans),
            "not_after": issued.info.not_after.isoformat(),
            "secret_ref": issued.secret_ref,
            "renew_policy": renew.value,
        }

    async def import_pem(
        self, principal: Principal, tenant_id: UUID, name: str, pem: str
    ) -> dict[str, Any]:
        """Check and store an uploaded bundle; return the ``certificate`` body for a change set.

        Raises:
            CertificateError: If the bundle is unusable.
        """
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, "manage", "certificate")
        issued = await self._store(tenant_id, name, pem)
        async with admin_session(self._db.admin) as session:
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="certificate.imported",
                object_ref=f"certificate:{name}",
                diff={"sans": list(issued.info.sans), "secret_ref": issued.secret_ref},
            )
        return {"name": name} | self._body(issued, dm.CertSource.UPLOAD, dm.RenewPolicy.MANUAL)

    async def import_pkcs12(
        self,
        principal: Principal,
        tenant_id: UUID,
        name: str,
        pkcs12_b64: str,
        password: str | None,
    ) -> dict[str, Any]:
        """Unpack an uploaded PKCS#12 file, then store it like :meth:`import_pem`.

        The password is used for decryption only: it is not logged, audited or stored, and the
        stored secret is the same unencrypted PEM bundle a PEM import produces.

        Raises:
            FieldErrors: With the field path when the file or password is wrong.
            CertificateError: If the unpacked bundle is unusable.
        """
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, "manage", "certificate")
        try:
            pem = pkcs12_to_pem(pkcs12_b64, password)
        except Pkcs12Error as exc:
            errors = FieldErrors()
            errors.add(exc.path, exc.code, str(exc))
            raise errors from None
        return await self.import_pem(principal, tenant_id, name, pem)

    async def replace(
        self,
        principal: Principal,
        tenant_id: UUID,
        certificate_id: UUID,
        *,
        pem: str | None,
        pkcs12_b64: str | None,
        password: str | None,
    ) -> tuple[dict[str, Any], int]:
        """Check and store new material for an existing certificate.

        The object keeps its id (so every virtual service that uses it keeps working) and name; its
        secret reference, names, expiry and source change, and renewal becomes manual because the
        material is now an upload.

        Returns:
            The ``certificate`` update body and the version it must be applied against.

        Raises:
            NotFoundError: If the certificate does not exist.
            FieldErrors: With the field path when a PKCS#12 file or password is wrong.
            CertificateError: If the material is unusable.
        """
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, "manage", "certificate")
        existing = next(
            (c for c in (await self._snapshot(tenant_id)).certificates if c.id == certificate_id),
            None,
        )
        if existing is None:
            raise NotFoundError(f"certificate {certificate_id} does not exist")
        if pkcs12_b64 is not None:
            data = await self.import_pkcs12(
                principal, tenant_id, existing.name, pkcs12_b64, password
            )
        else:
            data = await self.import_pem(principal, tenant_id, existing.name, pem or "")
        data.pop("name")
        return data, existing.version

    # -- issuing ----------------------------------------------------------------------------------

    async def _obtain(self, source: dm.CertSource, sans: tuple[str, ...]) -> str:
        key_pem, csr_pem = generate_key_and_csr(sans)
        try:
            if source is dm.CertSource.ACME:
                if self._acme is None:
                    raise CertificateError("ACME is not configured")
                return await self._acme.issue(csr_pem) + key_pem
            if source is dm.CertSource.CLOUDFLARE_ORIGIN:
                if self._cloudflare is None:
                    raise CertificateError("Cloudflare is not configured")
                return await self._cloudflare.issue_origin_ca(sans, csr_pem) + "\n" + key_pem
        except (AcmeError, CloudflareError) as exc:
            raise CertificateError(str(exc)) from exc
        raise CertificateError(f"certificates from {source.value} cannot be issued automatically")

    async def _snapshot(self, tenant_id: UUID) -> Snapshot:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            return await load_snapshot(session)

    async def _submit(self, tenant_id: UUID, summary: str, ops: list[ChangeOp]) -> UUID:
        """Open a change set with ``ops`` and queue it (or leave it for approval)."""
        who = _system(tenant_id)
        opened = await self._change_sets.open(who, tenant_id, summary)
        change_set_id = opened.record.id
        await self._change_sets.add_ops(who, tenant_id, change_set_id, ops)
        try:
            await self._change_sets.apply(who, tenant_id, change_set_id)
        except PermissionDeniedError:
            await self._change_sets.request_approval(who, tenant_id, change_set_id)
            log.warning("change set %s waits for approval: %s", change_set_id, summary)
        return change_set_id

    async def issue(
        self,
        tenant_id: UUID,
        name: str,
        source: dm.CertSource,
        sans: tuple[str, ...],
        *,
        renew: dm.RenewPolicy = dm.RenewPolicy.AUTO,
    ) -> UUID:
        """Obtain a certificate and create or update the ``certificate`` object.

        Returns:
            The id of the change set that was opened.

        Raises:
            CertificateError: If issuance or storing fails (nothing is changed).
        """
        pem = await self._obtain(source, sans)
        issued = await self._store(tenant_id, name, pem)
        existing = next(
            (c for c in (await self._snapshot(tenant_id)).certificates if c.name == name), None
        )
        body = self._body(issued, source, renew)
        if existing is None:
            op = ChangeOp(
                object_type=ObjectType.CERTIFICATE,
                object_id=uuid7(),
                action=Action.CREATE,
                body={"name": name} | body,
            )
        else:
            op = ChangeOp(
                object_type=ObjectType.CERTIFICATE,
                object_id=existing.id,
                action=Action.UPDATE,
                body=body,
                expected_version=existing.version,
            )
        return await self._submit(tenant_id, f"issue certificate {name}", [op])

    async def renew_due(self, *, now: datetime | None = None) -> list[str]:
        """Renew every automatic certificate that expires within 30 days.

        A failure for one certificate is logged and audited, and does not stop the others.

        Returns:
            ``tenant/certificate`` labels that were renewed.
        """
        now = now or datetime.now(UTC)
        async with admin_session(self._db.admin) as session:
            tenants = list((await session.execute(select(TenantRow.id, TenantRow.name))).all())
        renewed: list[str] = []
        for tenant_id, tenant_name in tenants:
            for cert in (await self._snapshot(tenant_id)).certificates:
                due = (
                    cert.renew_policy is dm.RenewPolicy.AUTO
                    and cert.source in {dm.CertSource.ACME, dm.CertSource.CLOUDFLARE_ORIGIN}
                    and cert.not_after - RENEW_BEFORE <= now
                )
                if not due:
                    continue
                try:
                    await self.issue(tenant_id, cert.name, cert.source, cert.sans)
                    renewed.append(f"{tenant_name}/{cert.name}")
                except (CertificateError, ValueError, PermissionError) as exc:
                    log.error("renewing %s/%s failed: %s", tenant_name, cert.name, exc)
                    async with admin_session(self._db.admin) as session:
                        await audit_log.record(
                            session,
                            tenant_id=tenant_id,
                            actor="system:certs",
                            action="certificate.renewal_failed",
                            object_ref=f"certificate:{cert.name}",
                            diff={"error": str(exc)},
                        )
        return renewed

    # -- Cloudflare allow list --------------------------------------------------------------------

    async def sync_cloudflare_ips(self) -> list[str]:
        """Update every allow list marked ``managed_by: cloudflare`` to the published ranges.

        Returns:
            Labels of the lists that changed (each as one change set).
        """
        if self._cloudflare is None:
            raise CertificateError("Cloudflare is not configured")
        ranges = sorted(self._cloudflare_ranges(await self._cloudflare.ip_ranges()))
        async with admin_session(self._db.admin) as session:
            tenants = list((await session.execute(select(TenantRow.id, TenantRow.name))).all())
        changed: list[str] = []
        for tenant_id, tenant_name in tenants:
            for policy in (await self._snapshot(tenant_id)).policy_sets:
                if not (isinstance(policy, dm.AllowListPolicySet) and policy.managed_by):
                    continue
                if sorted(str(c) for c in policy.cidrs) == ranges:
                    continue
                op = ChangeOp(
                    object_type=ObjectType.POLICY_SET,
                    object_id=policy.id,
                    action=Action.UPDATE,
                    body={"cidrs": ranges},
                    expected_version=policy.version,
                )
                await self._submit(tenant_id, f"sync Cloudflare IP ranges into {policy.name}", [op])
                changed.append(f"{tenant_name}/{policy.name}")
        return changed

    @staticmethod
    def _cloudflare_ranges(ranges: Any) -> list[str]:
        import ipaddress

        return [str(ipaddress.ip_network(c)) for c in ranges.all]

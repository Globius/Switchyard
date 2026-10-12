"""Device enrollment, posture reports and short-lived device certificates (ADR 0196).

Flow: a tenant administrator creates an enrollment token for a ``posture`` policy set. The device
agent (``sy-posture``) generates an Ed25519 key and a certificate key, trades the token and the
public key for a device identity (``enroll``), and then reports its facts on a schedule. Every
report is signed with the device's Ed25519 key over the method, path, a millisecond timestamp and
the SHA-256 of the body; a timestamp that is not newer than the last accepted one, or is more than
five minutes off, is refused, so a captured report cannot be replayed. The controller checks the
facts against the policy and, when they hold and the report carries a certificate request, signs
a **client certificate** for ``cert_ttl_h`` hours. A port that trusts the policy's CA and an access
rule with ``posture.device_certificate`` then admit the device; when it stops being compliant (or
an administrator revokes it) no new certificate is issued and the old one expires.

The controller never sees a device's private keys; the certificate's names come from the device
record, not from the request.
"""

import base64
import binascii
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError
from sqlalchemy import select

from switchyard.adapters.pki import PkiError, PkiSigner
from switchyard.db import audit_log
from switchyard.db.models import PolicySetRow, TenantRow
from switchyard.db.ops_models import DeviceEnrollmentRow, DeviceRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.models import PosturePolicySet
from switchyard.domain.posture import PostureFacts, evaluate
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import NotFoundError, PermissionDeniedError
from switchyard.services.tokens import hash_token

TOKEN_PREFIX = "sydev_"  # noqa: S105 - a prefix, not a password
TOKEN_TTL = timedelta(hours=24)
#: A report whose timestamp is further than this from the controller's clock is refused.
MAX_SKEW = timedelta(minutes=5)
DEVICE_NAME_MAX = 63

PkiForRole = Callable[[str], PkiSigner | None]


class DeviceAuthError(Exception):
    """The token, device or signature is not acceptable.

    One message for every credential problem, so the endpoint does not reveal which it was.
    """


class DeviceRequestError(Exception):
    """The request is authentic but unusable (bad facts, a name in use, a bad CSR)."""


@dataclass(frozen=True)
class Enrolled:
    """What a device receives when it enrolls."""

    device_id: UUID
    tenant: str
    name: str
    policy: str
    cert_ttl_h: int


@dataclass(frozen=True)
class ReportResult:
    """The answer to a posture report.

    Attributes:
        compliant: The facts meet the policy.
        failures: Codes of the requirements that failed.
        certificate: The new client certificate (PEM) when the device is compliant and sent a CSR.
        ca: The CA that signed it.
        not_after: When the certificate expires.
        next_report_s: When the agent should report next (a third of the certificate's life).
    """

    compliant: bool
    failures: list[str]
    certificate: str | None
    ca: str | None
    not_after: datetime | None
    next_report_s: int


def canonical(method: str, path: str, timestamp_ms: int, body: bytes) -> bytes:
    r"""The bytes a device signs: method, path, timestamp (ms) and SHA-256 of the body.

    Example:
        >>> canonical("POST", "/device/v1/report", 1700000000000, b"{}")[:34]
        b'POST\\n/device/v1/report\\n1700000000'
    """
    digest = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp_ms}\n{digest}".encode()


def parse_public_key(value: str) -> Ed25519PublicKey:
    """Decode a base64 Ed25519 public key (32 raw bytes).

    Raises:
        DeviceRequestError: If it is not one.
    """
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) != 32:
            raise ValueError("length")
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, binascii.Error) as exc:
        raise DeviceRequestError("the public key must be 32 bytes of Ed25519, base64") from exc


class DeviceService:
    """Creates enrollment tokens, enrolls devices, checks reports and issues certificates."""

    def __init__(
        self,
        db: Databases,
        pki_for_role: PkiForRole,
        *,
        clock: Clock = utcnow,
    ) -> None:
        """Create the service.

        Args:
            db: Database handles (device calls have no tenant yet and use the admin role).
            pki_for_role: Returns the signer for a policy's ``pki_role`` (``None``: no PKI).
            clock: Time source.
        """
        self._db = db
        self._pki_for_role = pki_for_role
        self._clock = clock

    # -- administration -------------------------------------------------------------------

    async def create_enrollment(
        self,
        principal: Principal,
        tenant_id: UUID,
        policy_id: UUID,
        label: str = "",
        *,
        request_id: str | None = None,
    ) -> tuple[str, datetime]:
        """Create a one-time enrollment token for a posture policy. Returns ``(token, expires)``."""
        self._check(principal, tenant_id)
        require(principal, "manage", "device")
        token = TOKEN_PREFIX + secrets.token_urlsafe(24)
        expires = self._clock() + TOKEN_TTL
        async with tenant_session(self._db.app, [tenant_id]) as session:
            policy = (
                await session.execute(
                    select(PolicySetRow).where(
                        PolicySetRow.id == policy_id, PolicySetRow.kind == "posture"
                    )
                )
            ).scalar_one_or_none()
            if policy is None:
                raise NotFoundError(f"posture policy set {policy_id} does not exist")
            session.add(
                DeviceEnrollmentRow(
                    id=uuid7(),
                    tenant_id=tenant_id,
                    policy_id=policy_id,
                    label=label[:80],
                    token_hash=hash_token(token),
                    expires_at=expires,
                    created_by=principal.actor,
                )
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="device.enrollment_created",
                object_ref=f"policy_set:{policy_id}",
                request_id=request_id,
            )
        return token, expires

    async def list_devices(self, principal: Principal, tenant_id: UUID) -> list[DeviceRow]:
        """The tenant's devices, newest enrollment first."""
        self._check(principal, tenant_id)
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = await session.execute(select(DeviceRow).order_by(DeviceRow.enrolled_at.desc()))
            return list(rows.scalars())

    async def get_device(self, principal: Principal, tenant_id: UUID, device_id: UUID) -> DeviceRow:
        """One device. Raises :class:`NotFoundError` if it is not in the tenant."""
        self._check(principal, tenant_id)
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = (
                await session.execute(select(DeviceRow).where(DeviceRow.id == device_id))
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"device {device_id} does not exist")
            return row

    async def revoke(
        self,
        principal: Principal,
        tenant_id: UUID,
        device_id: UUID,
        *,
        remove: bool = False,
        request_id: str | None = None,
    ) -> None:
        """Stop issuing certificates to a device; with ``remove`` forget it as well.

        The certificate it holds works until it expires (at most ``cert_ttl_h`` hours).
        """
        self._check(principal, tenant_id)
        require(principal, "manage", "device")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = (
                await session.execute(
                    select(DeviceRow).where(DeviceRow.id == device_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"device {device_id} does not exist")
            if remove:
                await session.delete(row)
            else:
                row.status = "revoked"
                row.revoked_at = self._clock()
                row.compliant = False
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="device.removed" if remove else "device.revoked",
                object_ref=f"device:{device_id}",
                diff={"name": row.name, "cert_serial": row.cert_serial},
                request_id=request_id,
            )

    # -- the device side ------------------------------------------------------------------

    async def enroll(self, token: str, name: str, public_key: str, owner: str = "") -> Enrolled:
        """Trade a one-time token and the device's public key for a device identity.

        Raises:
            DeviceAuthError: For any bad, used or expired token.
            DeviceRequestError: If the key or the name is unusable or the name is taken.
        """
        if not token.startswith(TOKEN_PREFIX):
            raise DeviceAuthError("invalid or expired token")
        parse_public_key(public_key)
        if not name or len(name) > DEVICE_NAME_MAX or not _valid_name(name):
            raise DeviceRequestError("the device name must be 1 to 63 of letters, digits, . _ -")
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(DeviceEnrollmentRow)
                    .where(DeviceEnrollmentRow.token_hash == hash_token(token))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.used_at is not None or row.expires_at <= now:
                raise DeviceAuthError("invalid or expired token")
            taken = (
                await session.execute(
                    select(DeviceRow.id).where(
                        DeviceRow.tenant_id == row.tenant_id, DeviceRow.name == name
                    )
                )
            ).first()
            if taken is not None:
                raise DeviceRequestError(f"a device named {name} is already enrolled")
            policy_row = (
                await session.execute(
                    select(PolicySetRow).where(
                        PolicySetRow.tenant_id == row.tenant_id, PolicySetRow.id == row.policy_id
                    )
                )
            ).scalar_one()
            policy = _policy(policy_row)
            tenant = (
                await session.execute(select(TenantRow).where(TenantRow.id == row.tenant_id))
            ).scalar_one()
            row.used_at = now
            device_id = uuid7()
            session.add(
                DeviceRow(
                    id=device_id,
                    tenant_id=row.tenant_id,
                    policy_id=row.policy_id,
                    name=name,
                    owner=owner[:120],
                    public_key=public_key,
                    status="active",
                    failures=[],
                    last_ts=0,
                    enrolled_at=now,
                )
            )
            await audit_log.record(
                session,
                tenant_id=row.tenant_id,
                actor=f"device:{name}",
                action="device.enrolled",
                object_ref=f"device:{device_id}",
                diff={"policy": policy_row.name},
            )
            return Enrolled(device_id, tenant.name, name, policy_row.name, policy.cert_ttl_h)

    async def report(
        self,
        device_id: UUID,
        *,
        timestamp_ms: int,
        signature: str,
        method: str,
        path: str,
        body: bytes,
    ) -> ReportResult:
        """Check a signed posture report, evaluate it and issue a certificate if it holds.

        Raises:
            DeviceAuthError: If the device is unknown or revoked, the signature or timestamp is
                wrong, or the report is a replay.
            DeviceRequestError: If the facts or the certificate request are unusable.
            PkiError: If signing fails.
        """
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            device = (
                await session.execute(
                    select(DeviceRow).where(DeviceRow.id == device_id).with_for_update()
                )
            ).scalar_one_or_none()
            if device is None or device.status != "active":
                raise DeviceAuthError("unknown device or bad signature")
            self._verify(device, timestamp_ms, signature, method, path, body, now)
            device.last_ts = timestamp_ms
            facts, csr = _parse_report(body)
            policy_row = (
                await session.execute(
                    select(PolicySetRow).where(
                        PolicySetRow.tenant_id == device.tenant_id,
                        PolicySetRow.id == device.policy_id,
                    )
                )
            ).scalar_one()
            policy = _policy(policy_row)
            failures = evaluate(policy.requirements, facts)
            compliant = not failures
            changed = device.compliant is not compliant
            device.compliant, device.failures = compliant, failures
            device.facts = facts.model_dump(mode="json")
            device.last_report_at = now
            interval = max(policy.cert_ttl_h * 3600 // 3, 300)
            certificate = ca = None
            not_after = None
            if compliant and csr is not None:
                signer = self._pki_for_role(policy.pki_role)
                if signer is None:
                    raise DeviceRequestError("device certificates are not configured")
                tenant = (
                    await session.execute(select(TenantRow).where(TenantRow.id == device.tenant_id))
                ).scalar_one()
                ttl = timedelta(hours=policy.cert_ttl_h)
                signed = await signer.sign(
                    csr,
                    common_name=device.name,
                    ip_sans=(),
                    ttl=ttl,
                    usage="client",
                    uri_sans=(f"urn:switchyard:device:{tenant.name}:{device.id}",),
                )
                device.cert_serial, device.cert_not_after = signed.serial, now + ttl
                certificate, ca, not_after = signed.cert_pem, signed.ca_pem, now + ttl
            if changed:
                await audit_log.record(
                    session,
                    tenant_id=device.tenant_id,
                    actor=f"device:{device.name}",
                    action="device.compliant" if compliant else "device.non_compliant",
                    object_ref=f"device:{device.id}",
                    diff={"failures": failures},
                )
            return ReportResult(compliant, failures, certificate, ca, not_after, interval)

    # -- helpers --------------------------------------------------------------------------

    @staticmethod
    def _check(principal: Principal, tenant_id: UUID) -> None:
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")

    def _verify(
        self,
        device: DeviceRow,
        timestamp_ms: int,
        signature: str,
        method: str,
        path: str,
        body: bytes,
        now: datetime,
    ) -> None:
        skew = abs(timestamp_ms - int(now.timestamp() * 1000))
        if skew > MAX_SKEW.total_seconds() * 1000 or timestamp_ms <= device.last_ts:
            raise DeviceAuthError("unknown device or bad signature")
        try:
            key = parse_public_key(device.public_key)
            key.verify(
                base64.b64decode(signature, validate=True),
                canonical(method, path, timestamp_ms, body),
            )
        except (InvalidSignature, binascii.Error, ValueError, DeviceRequestError) as exc:
            raise DeviceAuthError("unknown device or bad signature") from exc


def _valid_name(name: str) -> bool:
    return all(c.isalnum() or c in "._-" for c in name) and name[0].isalnum() and name.isascii()


def _policy(row: PolicySetRow) -> PosturePolicySet:
    data: dict[str, Any] = {
        "id": row.id,
        "version": row.version,
        "tenant_id": row.tenant_id,
        "name": row.name,
        "kind": "posture",
        **(row.config or {}),
    }
    return PosturePolicySet.model_validate(data)


def _parse_report(body: bytes) -> tuple[PostureFacts, str | None]:
    """The facts and the optional certificate request of a report body (JSON)."""
    if len(body) > 32_768:
        raise DeviceRequestError("the report is too large")
    try:
        document = json.loads(body)
        if not isinstance(document, dict) or set(document) - {"facts", "csr"}:
            raise ValueError("unexpected fields")
        facts = PostureFacts.model_validate(document["facts"])
        csr = document.get("csr")
        if csr is not None and (not isinstance(csr, str) or len(csr) > 16_384):
            raise ValueError("bad csr")
    except (ValueError, KeyError, ValidationError) as exc:
        raise DeviceRequestError(f"unusable report: {exc}") from exc
    return facts, csr


__all__ = [
    "DeviceAuthError",
    "DeviceRequestError",
    "DeviceService",
    "Enrolled",
    "PkiError",
    "ReportResult",
    "canonical",
]

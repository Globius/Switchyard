# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent enrollment: trade a one-time token for a short-lived server certificate.

Flow (backend design, "Agent"): an administrator creates a token for an engine; cloud-init hands it
to ``sy-agent enroll``, which generates its own key and sends a CSR. The controller checks the
token, signs a certificate whose names come from the *engine record* (never from the CSR), and
returns it with the CA and the public key that verifies configuration bundles. Along with the
certificate the agent gets a rotating renewal token, so it can renew without a human.

Tokens are random, stored only as SHA-256, work once, and expire. Using a token and issuing the
next one happen in one transaction with the signing call: a signing failure leaves the token valid.
"""

import logging
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update

from switchyard.adapters.pki import PkiError, PkiSigner
from switchyard.db import audit_log
from switchyard.db.ops_models import EnrollmentTokenRow, ServiceEngineRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import NotFoundError, PermissionDeniedError
from switchyard.services.tokens import hash_token

log = logging.getLogger(__name__)

#: Names no engine certificate carries: the controller's own (default ``controller_cn``).
RESERVED_NAMES = ("switchyard-controller",)
ENROLL_PREFIX = "syenr_"
RENEW_PREFIX = "syren_"
ENROLL_TTL = timedelta(hours=1)
RENEW_TTL = timedelta(days=90)
CERT_TTL = timedelta(days=7)


class EnrollmentError(Exception):
    """The token is unknown, used, expired or the request is unusable.

    One message for every credential problem, so the endpoint does not reveal which it was.
    """


@dataclass(frozen=True)
class Enrollment:
    """What an agent receives after enrolling or renewing."""

    engine: str
    cert_pem: str
    ca_pem: str
    bundle_public_key: str
    renewal_token: str
    not_after: datetime
    serial: str


class EnrollmentService:
    """Creates enrollment tokens and redeems them for certificates."""

    def __init__(
        self,
        db: Databases,
        pki: PkiSigner,
        bundle_public_key: str,
        *,
        cert_ttl: timedelta = CERT_TTL,
        reserved_names: Iterable[str] = (),
        clock: Clock = utcnow,
    ) -> None:
        """Create the service.

        Args:
            db: Database handles (redeeming uses the admin role: the caller has no tenant yet).
            pki: Signs the agent certificates.
            bundle_public_key: Base64 Ed25519 public key agents use to verify bundles.
            cert_ttl: Lifetime of issued certificates.
            reserved_names: Names no engine certificate may carry, in addition to
                :data:`RESERVED_NAMES`: the common names agents accept from the controller
                (``sy-agent -controller-cn``). An engine is named by a tenant administrator, and a
                certificate called ``switchyard-controller`` would be accepted by every agent as
                the controller's.
            clock: Time source.
        """
        self._db = db
        self._pki = pki
        self._bundle_key = bundle_public_key
        self._cert_ttl = cert_ttl
        self._reserved = {n.strip().lower() for n in (*RESERVED_NAMES, *reserved_names)}
        self._clock = clock

    async def create_token(
        self,
        principal: Principal,
        tenant_id: UUID,
        engine_id: UUID,
        *,
        request_id: str | None = None,
    ) -> tuple[str, datetime]:
        """Create a one-time token for an engine. Shown once; returns ``(token, expires_at)``."""
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, "create", "enrollment_token")
        token = ENROLL_PREFIX + secrets.token_urlsafe(24)
        expires = self._clock() + ENROLL_TTL
        async with tenant_session(self._db.app, [tenant_id]) as session:
            engine = (
                await session.execute(
                    select(ServiceEngineRow).where(ServiceEngineRow.id == engine_id)
                )
            ).scalar_one_or_none()
            if engine is None:
                raise NotFoundError(f"engine {engine_id} does not exist")
            if not engine.mgmt_ip:
                raise ValueError("the engine needs a management address before it can enroll")
            await session.execute(
                update(EnrollmentTokenRow)
                .where(
                    EnrollmentTokenRow.engine_id == engine_id,
                    EnrollmentTokenRow.kind == "enroll",
                    EnrollmentTokenRow.used_at.is_(None),
                )
                .values(used_at=self._clock())
            )
            session.add(
                EnrollmentTokenRow(
                    id=uuid7(),
                    tenant_id=tenant_id,
                    engine_id=engine_id,
                    kind="enroll",
                    token_hash=hash_token(token),
                    expires_at=expires,
                    created_by=principal.actor,
                )
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="engine.enrollment_token_created",
                object_ref=f"engine:{engine_id}",
                request_id=request_id,
            )
        return token, expires

    async def redeem(self, token: str, csr_pem: str) -> Enrollment:
        """Exchange an enrollment or renewal token and a CSR for a certificate.

        Raises:
            EnrollmentError: For any bad, used or expired token.
            PkiError: If the CSR is unusable or signing fails (the token stays valid).
        """
        if not token.startswith((ENROLL_PREFIX, RENEW_PREFIX)):
            raise EnrollmentError("invalid or expired token")
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(EnrollmentTokenRow)
                    .where(EnrollmentTokenRow.token_hash == hash_token(token))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.used_at is not None or row.expires_at <= now:
                raise EnrollmentError("invalid or expired token")
            engine = (
                await session.execute(
                    select(ServiceEngineRow).where(ServiceEngineRow.id == row.engine_id)
                )
            ).scalar_one()
            if not engine.mgmt_ip:
                raise EnrollmentError("invalid or expired token")
            if engine.name.lower() in self._reserved:
                log.warning("refusing to issue a certificate named %s to an engine", engine.name)
                raise EnrollmentError("invalid or expired token")
            address = str(engine.mgmt_ip).split("/")[0]
            signed = await self._pki.sign(
                csr_pem, common_name=engine.name, ip_sans=(address,), ttl=self._cert_ttl
            )
            row.used_at = now
            renewal = RENEW_PREFIX + secrets.token_urlsafe(24)
            session.add(
                EnrollmentTokenRow(
                    id=uuid7(),
                    tenant_id=row.tenant_id,
                    engine_id=row.engine_id,
                    kind="renew",
                    token_hash=hash_token(renewal),
                    expires_at=now + RENEW_TTL,
                    created_by=f"agent:{engine.name}",
                )
            )
            engine.agent_cert_serial = signed.serial
            await audit_log.record(
                session,
                tenant_id=row.tenant_id,
                actor=f"agent:{engine.name}",
                action="engine.enrolled" if row.kind == "enroll" else "engine.cert_renewed",
                object_ref=f"engine:{row.engine_id}",
                diff={"serial": signed.serial},
            )
            return Enrollment(
                engine=engine.name,
                cert_pem=signed.cert_pem,
                ca_pem=signed.ca_pem,
                bundle_public_key=self._bundle_key,
                renewal_token=renewal,
                not_after=now + self._cert_ttl,
                serial=signed.serial,
            )


__all__ = ["Enrollment", "EnrollmentError", "EnrollmentService", "PkiError"]

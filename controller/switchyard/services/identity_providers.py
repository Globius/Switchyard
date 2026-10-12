# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Identity providers as an administered resource (``/identity-providers``) and test sign-in.

A system administrator creates, changes and deletes OIDC and SAML providers through the API. Client
secrets are write-only: the controller stores them in the secrets backend and keeps only the
reference. A SAML provider can be configured from its metadata (URL or XML), and can sign its
requests with a key the controller generates and keeps in the secrets backend.

A *test sign-in* runs the real login against a provider but creates no session and no user: the
outcome (claims, groups, what the current mappings would grant) is stored for five minutes and read
once. See ADR 0032.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from cryptography import x509
from sqlalchemy import delete, select

from switchyard.adapters.secrets import SecretsReader, SecretsWriter
from switchyard.db import audit_log
from switchyard.db.auth_models import IdentityProviderRow, IdpTestRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.validators import validate_name, validate_secret_ref
from switchyard.ids import uuid7
from switchyard.services import saml
from switchyard.services.changesets import require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import (
    AlreadyExistsError,
    FieldErrors,
    NotFoundError,
    PreconditionFailedError,
)
from switchyard.services.oidc import OidcClient, OidcError

TEST_TTL = timedelta(minutes=5)
METADATA_LIMIT = 1_000_000
_CLAIM = re.compile(r"[\w.:/\-]{1,200}")
_SCOPES = re.compile(r"[\w.:/\- ]{1,500}")
OIDC_FIELDS = {
    "issuer_url",
    "client_id",
    "client_secret",
    "client_secret_ref",
    "scopes",
    "username_claim",
    "email_claim",
    "groups_claim",
}
SAML_FIELDS = {
    "idp_metadata_url",
    "idp_metadata_xml",
    "idp_entity_id",
    "sso_url",
    "signing_certificates",
    "sp_entity_id",
    "name_id_format",
    "sign_requests",
    "sp_signing_key_ref",
    "attribute_username",
    "attribute_email",
    "attribute_groups",
}
#: Claims that carry tokens or proof material; never shown in a test result.
_HIDDEN_CLAIMS = {"nonce", "at_hash", "c_hash", "s_hash", "jti"}


@dataclass(frozen=True)
class IdentityProviderRecord:
    """An identity provider as the API shows it (never a secret).

    ``settings`` holds the protocol-specific fields. ``warnings`` is only filled on create and
    update, for example when the OIDC discovery document could not be reached.
    """

    id: UUID
    name: str
    protocol: str
    enabled: bool
    version: int
    created_at: datetime
    settings: dict[str, Any]
    warnings: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class TestResult:
    """The state of a test sign-in. ``result`` is set once the provider answered."""

    status: str
    result: dict[str, Any] | None = None


def clean_claims(claims: dict[str, Any]) -> dict[str, Any]:
    """Claims safe to show: no tokens, proofs or secrets, and no huge values."""
    out: dict[str, Any] = {}
    for key, value in claims.items():
        lowered = key.lower()
        if (
            key in _HIDDEN_CLAIMS
            or "token" in lowered
            or "secret" in lowered
            or "signature" in lowered
        ):
            continue
        out[key] = value if len(str(value)) <= 1000 else str(value)[:1000] + "..."
    return out


def _https(url: str) -> bool:
    return url.startswith(("https://", "http://localhost"))


def _pem_certificate(value: str) -> bool:
    try:
        x509.load_pem_x509_certificate(value.encode())
    except ValueError:
        return False
    return True


class IdentityProviderService:
    """Create, change, delete and test identity providers (system administrators)."""

    def __init__(
        self,
        db: Databases,
        oidc: OidcClient,
        secrets_reader: SecretsReader | None = None,
        secrets_writer: SecretsWriter | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Clock = utcnow,
    ) -> None:
        """Create the service.

        Args:
            db: Database handles (all of this runs on the admin role).
            oidc: OpenID Connect client, used to probe discovery documents.
            secrets_reader: Reads secret references (SAML request signing keys).
            secrets_writer: Stores write-only secrets (client secrets, generated signing keys).
            http: Client used to fetch SAML metadata by URL (replaceable in tests).
            clock: Time source.
        """
        self._db = db
        self._oidc = oidc
        self._reader = secrets_reader
        self._writer = secrets_writer
        self._http = http or httpx.AsyncClient(timeout=10.0, follow_redirects=False)
        self._clock = clock
        self._pending_keys: dict[UUID, tuple[str, str]] = {}

    # -- reads ------------------------------------------------------------------------------------

    async def list_providers(self, principal: Principal) -> list[IdentityProviderRecord]:
        """All providers, by name."""
        require(principal, "manage", "identity_provider")
        async with admin_session(self._db.admin) as session:
            rows = (
                await session.execute(
                    select(IdentityProviderRow).order_by(IdentityProviderRow.name)
                )
            ).scalars()
            return [_record(r) for r in rows]

    async def get(self, principal: Principal, provider_id: UUID) -> IdentityProviderRecord:
        """One provider (``NotFoundError`` if it does not exist)."""
        require(principal, "manage", "identity_provider")
        async with admin_session(self._db.admin) as session:
            return _record(await self._row(session, provider_id))

    # -- writes -----------------------------------------------------------------------------------

    async def create(
        self,
        principal: Principal,
        *,
        protocol: str,
        name: str,
        enabled: bool,
        settings: dict[str, Any],
        request_id: str | None = None,
    ) -> IdentityProviderRecord:
        """Create a provider.

        Raises:
            FieldErrors: With the path of every invalid field.
            AlreadyExistsError: If the name is taken.
        """
        require(principal, "manage", "identity_provider")
        errors = FieldErrors()
        try:
            validate_name(name)
        except ValueError as exc:
            errors.add("name", "invalid", str(exc))
        provider_id = uuid7()
        row = IdentityProviderRow(id=provider_id, name=name, protocol=protocol, enabled=enabled)
        warnings = await self._apply(row, settings, errors, creating=True)
        errors.raise_if_any()
        async with admin_session(self._db.admin) as session:
            taken = (
                await session.execute(
                    select(IdentityProviderRow.id).where(IdentityProviderRow.name == name)
                )
            ).first()
            if taken is not None:
                raise AlreadyExistsError(f"identity provider {name!r} already exists")
            session.add(row)
            await session.flush()
            await self._store_pending_secrets(row, settings)
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="idp.created",
                object_ref=f"idp:{name}",
                diff={"protocol": protocol, "enabled": enabled},
                request_id=request_id,
            )
            await session.refresh(row)
            return _record(row, tuple(warnings))

    async def update(
        self,
        principal: Principal,
        provider_id: UUID,
        *,
        expected_version: int,
        changes: dict[str, Any],
        request_id: str | None = None,
    ) -> IdentityProviderRecord:
        """Change a provider (``protocol`` cannot change). Only the fields sent change.

        Raises:
            PreconditionFailedError: If ``expected_version`` is stale.
            FieldErrors: With the path of every invalid field.
        """
        require(principal, "manage", "identity_provider")
        async with admin_session(self._db.admin) as session:
            row = await self._row(session, provider_id, for_update=True)
            if row.version != expected_version:
                raise PreconditionFailedError(
                    f"identity provider is at version {row.version}, not {expected_version}"
                )
            errors = FieldErrors()
            common = {k: changes[k] for k in ("name", "enabled") if k in changes}
            if "name" in common:
                try:
                    validate_name(str(common["name"]))
                except ValueError as exc:
                    errors.add("name", "invalid", str(exc))
            settings = {
                k: v for k, v in changes.items() if k not in {"name", "enabled", "protocol"}
            }
            warnings = await self._apply(row, settings, errors, creating=False)
            errors.raise_if_any()
            if "name" in common and common["name"] != row.name:
                clash = (
                    await session.execute(
                        select(IdentityProviderRow.id).where(
                            IdentityProviderRow.name == common["name"],
                            IdentityProviderRow.id != row.id,
                        )
                    )
                ).first()
                if clash is not None:
                    raise AlreadyExistsError(f"identity provider {common['name']!r} already exists")
                row.name = str(common["name"])
            if "enabled" in common:
                row.enabled = bool(common["enabled"])
            await self._store_pending_secrets(row, settings)
            row.version += 1
            await session.flush()
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="idp.updated",
                object_ref=f"idp:{row.name}",
                diff={"fields": sorted(changes)},  # names only: values may be secrets
                request_id=request_id,
            )
            return _record(row, tuple(warnings))

    async def delete(
        self,
        principal: Principal,
        provider_id: UUID,
        *,
        expected_version: int,
        request_id: str | None = None,
    ) -> None:
        """Delete a provider with its mappings, users and sessions."""
        require(principal, "manage", "identity_provider")
        async with admin_session(self._db.admin) as session:
            row = await self._row(session, provider_id, for_update=True)
            if row.version != expected_version:
                raise PreconditionFailedError(
                    f"identity provider is at version {row.version}, not {expected_version}"
                )
            name = row.name
            await session.delete(row)
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="idp.deleted",
                object_ref=f"idp:{name}",
                request_id=request_id,
            )

    # -- test sign-in -----------------------------------------------------------------------------

    async def new_test(self, principal: Principal, provider_id: UUID) -> UUID:
        """Record a test sign-in and return its id (the caller starts the login with it)."""
        require(principal, "manage", "identity_provider")
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            row = await self._row(session, provider_id)
            await session.execute(delete(IdpTestRow).where(IdpTestRow.expires_at < now))
            test_id = uuid7()
            session.add(
                IdpTestRow(
                    id=test_id,
                    provider_id=row.id,
                    created_by=principal.actor,
                    status="pending",
                    created_at=now,
                    expires_at=now + TEST_TTL,
                )
            )
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="idp.test_started",
                object_ref=f"idp:{row.name}",
            )
            return test_id

    async def read_test(self, principal: Principal, provider_id: UUID, test_id: UUID) -> TestResult:
        """The state of a test: ``pending``, or ``done`` with the result (which is deleted).

        Raises:
            NotFoundError: If it does not exist, has expired or was already read.
        """
        require(principal, "manage", "identity_provider")
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(IdpTestRow)
                    .where(IdpTestRow.id == test_id, IdpTestRow.provider_id == provider_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.expires_at <= now:
                raise NotFoundError("test sign-in not found, expired or already read")
            if row.status == "pending":
                return TestResult("pending")
            result = dict(row.result or {})
            await session.delete(row)
            return TestResult("done", result)

    # -- helpers ----------------------------------------------------------------------------------

    @staticmethod
    async def _row(session: Any, provider_id: UUID, *, for_update: bool = False) -> Any:
        query = select(IdentityProviderRow).where(IdentityProviderRow.id == provider_id)
        if for_update:
            query = query.with_for_update()
        row = (await session.execute(query)).scalar_one_or_none()
        if row is None:
            raise NotFoundError("identity provider not found")
        return row

    async def _apply(
        self,
        row: IdentityProviderRow,
        settings: dict[str, Any],
        errors: FieldErrors,
        *,
        creating: bool,
    ) -> list[str]:
        """Validate ``settings`` and copy them onto ``row``; returns warnings."""
        warnings: list[str] = []
        known = OIDC_FIELDS if row.protocol == "oidc" else SAML_FIELDS
        for key in settings:
            if key not in known:
                errors.add(key, "not_allowed", f"{key} does not apply to {row.protocol} providers")
        if row.protocol == "oidc":
            await self._apply_oidc(row, settings, errors, creating=creating, warnings=warnings)
        else:
            await self._apply_saml(row, settings, errors, creating=creating)
        return warnings

    async def _apply_oidc(
        self,
        row: IdentityProviderRow,
        s: dict[str, Any],
        errors: FieldErrors,
        *,
        creating: bool,
        warnings: list[str],
    ) -> None:
        if creating:
            for required in ("issuer_url", "client_id"):
                if not s.get(required):
                    errors.add(required, "required", f"{required} is required")
        if "issuer_url" in s and s["issuer_url"] is not None:
            if not _https(str(s["issuer_url"])):
                errors.add("issuer_url", "invalid", "issuer_url must be an https:// URL")
            else:
                row.issuer_url = str(s["issuer_url"]).rstrip("/")
        if s.get("client_id"):
            row.client_id = str(s["client_id"])
        if s.get("client_secret") and s.get("client_secret_ref"):
            errors.add(
                "client_secret", "conflict", "send client_secret or client_secret_ref, not both"
            )
        if s.get("client_secret_ref"):
            try:
                row.client_secret_ref = validate_secret_ref(str(s["client_secret_ref"]))
            except ValueError as exc:
                errors.add("client_secret_ref", "invalid", str(exc))
        if s.get("client_secret"):
            if self._writer is None:
                errors.add(
                    "client_secret",
                    "no_secrets_backend",
                    "the controller has no secrets backend to store a client secret; "
                    "send client_secret_ref instead",
                )
            elif not row.client_secret_ref:
                row.client_secret_ref = f"kv/idp/{row.name}"
        if "scopes" in s and s["scopes"] is not None:
            scopes = str(s["scopes"])
            if not _SCOPES.fullmatch(scopes) or "openid" not in scopes.split():
                errors.add("scopes", "invalid", "scopes must be space-separated and include openid")
            else:
                row.scopes = scopes
        for key in ("username_claim", "email_claim", "groups_claim"):
            if key in s and s[key] is not None:
                if not _CLAIM.fullmatch(str(s[key])):
                    errors.add(key, "invalid", f"{key} is not a valid claim name")
                else:
                    setattr(row, key, str(s[key]))
        if row.issuer_url and ("issuer_url" in s) and not errors.has("issuer_url"):
            try:
                await self._oidc.check_discovery(row.issuer_url)
            except OidcError as exc:
                warnings.append(f"discovery document not reachable: {exc}")

    async def _apply_saml(
        self, row: IdentityProviderRow, s: dict[str, Any], errors: FieldErrors, *, creating: bool
    ) -> None:
        saml_settings: dict[str, Any] = dict(row.saml or {})
        imported: saml.IdpMetadata | None = None
        xml, url = s.get("idp_metadata_xml"), s.get("idp_metadata_url")
        if xml and url:
            errors.add("idp_metadata_xml", "conflict", "send idp_metadata_url or idp_metadata_xml")
        elif xml or url:
            try:
                if url:
                    if not str(url).startswith("https://"):
                        raise saml.SamlError("idp_metadata_url must be an https:// URL")
                    saml_settings["idp_metadata_url"] = str(url)
                imported = saml.parse_idp_metadata(xml or await self._fetch(str(url)))
            except saml.SamlError as exc:
                errors.add("idp_metadata_url" if url else "idp_metadata_xml", "invalid", str(exc))
        values = dict(s)
        if imported is not None:
            # explicit values win; metadata fills the rest
            values.setdefault("idp_entity_id", imported.entity_id)
            values.setdefault("sso_url", imported.sso_url)
            values.setdefault("signing_certificates", list(imported.signing_certificates))
        for key in ("idp_entity_id", "sso_url"):
            if key in values and values[key] is not None:
                text = str(values[key]).strip()
                if not text or (key == "sso_url" and not _https(text)):
                    errors.add(
                        key,
                        "invalid",
                        f"{key} must be {'an https:// URL' if key == 'sso_url' else 'set'}",
                    )
                else:
                    saml_settings[key] = text
        if "signing_certificates" in values and values["signing_certificates"] is not None:
            certs = [str(c).strip() for c in values["signing_certificates"]]
            if not certs:
                errors.add(
                    "signing_certificates", "required", "at least one certificate is required"
                )
            for i, cert in enumerate(certs):
                if not _pem_certificate(cert):
                    errors.add(
                        f"signing_certificates.{i}", "invalid", "must be a PEM X.509 certificate"
                    )
            saml_settings["signing_certificates"] = certs
        for key in ("sp_entity_id", "name_id_format"):
            if values.get(key):
                saml_settings[key] = str(values[key])
        for key in ("attribute_username", "attribute_email", "attribute_groups"):
            if values.get(key) is not None:
                if not _CLAIM.fullmatch(str(values[key])):
                    errors.add(key, "invalid", f"{key} is not a valid attribute name")
                else:
                    saml_settings[key] = str(values[key])
        if values.get("sp_signing_key_ref"):
            try:
                saml_settings["sp_signing_key_ref"] = validate_secret_ref(
                    str(values["sp_signing_key_ref"])
                )
            except ValueError as exc:
                errors.add("sp_signing_key_ref", "invalid", str(exc))
        if "sign_requests" in values and values["sign_requests"] is not None:
            saml_settings["sign_requests"] = bool(values["sign_requests"])
        if creating:
            for required in ("idp_entity_id", "sso_url", "signing_certificates"):
                if not saml_settings.get(required):
                    errors.add(required, "required", f"{required} is required (or send metadata)")
        if saml_settings.get("sign_requests") and not errors:
            await self._ensure_signing_key(row, saml_settings, errors)
        row.saml = saml_settings

    async def _ensure_signing_key(
        self, row: IdentityProviderRow, saml_settings: dict[str, Any], errors: FieldErrors
    ) -> None:
        """Make sure request signing has a key reference and a published certificate."""
        if saml_settings.get("sp_signing_key_ref") and saml_settings.get("sp_signing_certificate"):
            return
        if self._writer is None:
            errors.add(
                "sign_requests",
                "no_secrets_backend",
                "the controller has no secrets backend to keep a signing key",
            )
            return
        ref = saml_settings.get("sp_signing_key_ref") or f"kv/idp/{row.name}/sp-signing-key"
        key_pem, cert_pem = saml.generate_sp_keypair(f"switchyard {row.name}", now=self._clock())
        saml_settings["sp_signing_key_ref"] = ref
        saml_settings["sp_signing_certificate"] = cert_pem.strip()
        self._pending_keys[row.id] = (ref, key_pem)

    async def _store_pending_secrets(
        self, row: IdentityProviderRow, settings: dict[str, Any]
    ) -> None:
        """Write the secrets collected while validating, now that the row is accepted."""
        if settings.get("client_secret") and self._writer is not None and row.client_secret_ref:
            await self._writer.put(row.client_secret_ref, str(settings["client_secret"]))
        pending = self._pending_keys.pop(row.id, None)
        if pending is not None and self._writer is not None:
            await self._writer.put(*pending)

    async def _fetch(self, url: str) -> str:
        try:
            response = await self._http.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise saml.SamlError("the metadata URL could not be fetched") from exc
        if len(response.content) > METADATA_LIMIT:
            raise saml.SamlError("the metadata document is too large")
        return response.text


def _record(row: IdentityProviderRow, warnings: tuple[str, ...] = ()) -> IdentityProviderRecord:
    if row.protocol == "oidc":
        settings: dict[str, Any] = {
            "issuer_url": row.issuer_url,
            "client_id": row.client_id,
            "client_secret_ref": row.client_secret_ref,
            "client_secret_set": bool(row.client_secret_ref),
            "scopes": row.scopes,
            "username_claim": row.username_claim,
            "email_claim": row.email_claim,
            "groups_claim": row.groups_claim,
        }
    else:
        s = row.saml or {}
        settings = {
            "idp_metadata_url": s.get("idp_metadata_url"),
            "idp_entity_id": s.get("idp_entity_id"),
            "sso_url": s.get("sso_url"),
            "signing_certificates": list(s.get("signing_certificates", [])),
            "sp_entity_id": s.get("sp_entity_id"),
            "name_id_format": s.get("name_id_format"),
            "sign_requests": bool(s.get("sign_requests", False)),
            "sp_signing_key_ref": s.get("sp_signing_key_ref"),
            "sp_signing_certificate": s.get("sp_signing_certificate"),
            "attribute_username": s.get("attribute_username", "username"),
            "attribute_email": s.get("attribute_email", "email"),
            "attribute_groups": s.get("attribute_groups", "groups"),
        }
    return IdentityProviderRecord(
        id=row.id,
        name=row.name,
        protocol=row.protocol,
        enabled=row.enabled,
        version=row.version,
        created_at=row.created_at,
        settings=settings,
        warnings=warnings,
    )


__all__ = [
    "IdentityProviderRecord",
    "IdentityProviderService",
    "TestResult",
    "clean_claims",
]

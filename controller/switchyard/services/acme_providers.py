"""ACME providers: the certificate authorities the controller may order from (ADR 0079).

A system administrator registers a provider once: a preset (Let's Encrypt, ZeroSSL, Google Trust
Services, Buypass, Keyfactor) or a custom directory, an e-mail address and how the controller
authenticates (nothing, External Account Binding, or OAuth for CAs behind an identity provider).
The controller creates the ACME account key itself and keeps it, the EAB key and the OAuth client
secret in the secrets backend; the database holds only references. Tenants choose a provider by name
when they issue a certificate; the default provider is used when they do not.
"""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from acme import challenges
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

from switchyard.adapters.acme import (
    AcmeAuth,
    AcmeError,
    AcmeIssuer,
    Eab,
    Solver,
    Stage,
    new_account_key,
)
from switchyard.adapters.oauth import ClientCredentials
from switchyard.adapters.secrets import SecretBackendError
from switchyard.db import audit_log
from switchyard.db.acme_models import AcmeProviderRow
from switchyard.db.models import CertificateRow
from switchyard.db.tenancy import admin_session
from switchyard.domain import validators as v
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import (
    AlreadyExistsError,
    ConflictError,
    FieldErrors,
    NotFoundError,
    PreconditionFailedError,
)

AUTH_MODES = ("none", "eab", "oauth")
_OAUTH_KEYS = ("token_url", "client_id", "scope", "audience", "bearer_on_acme", "eab_url")


@dataclass(frozen=True)
class Preset:
    """A well-known certificate authority.

    Attributes:
        key: Identifier used in API bodies.
        label: Name for people.
        directory_url: The ACME directory; ``None`` when the administrator must enter it.
        auth_mode: The authentication the CA usually needs.
        eab_required: The CA refuses accounts without External Account Binding.
        note: One sentence of guidance for the form.
    """

    key: str
    label: str
    directory_url: str | None
    auth_mode: str
    eab_required: bool
    note: str


PRESETS: tuple[Preset, ...] = (
    Preset(
        "letsencrypt",
        "Let's Encrypt",
        "https://acme-v02.api.letsencrypt.org/directory",
        "none",
        False,
        "Free certificates, rate limited; use staging for tests.",
    ),
    Preset(
        "letsencrypt_staging",
        "Let's Encrypt (staging)",
        "https://acme-staging-v02.api.letsencrypt.org/directory",
        "none",
        False,
        "Untrusted test certificates with high limits.",
    ),
    Preset(
        "zerossl",
        "ZeroSSL",
        "https://acme.zerossl.com/v2/DV90",
        "eab",
        True,
        "Create EAB credentials in the ZeroSSL dashboard.",
    ),
    Preset(
        "google",
        "Google Trust Services",
        "https://dv.acme-v02.api.pki.goog/directory",
        "eab",
        True,
        "Create EAB credentials with gcloud publicca external-account-keys create.",
    ),
    Preset(
        "buypass",
        "Buypass Go SSL",
        "https://api.buypass.com/acme/directory",
        "none",
        False,
        "Free 180-day certificates.",
    ),
    Preset(
        "keyfactor",
        "Keyfactor ACME",
        None,
        "oauth",
        True,
        "Enter your directory URL. The controller signs in to your identity provider and can "
        "fetch an EAB key from the Keyfactor key management endpoint.",
    ),
    Preset(
        "custom",
        "Other ACME server (step-ca, Pebble, ...)",
        None,
        "none",
        False,
        "Any RFC 8555 directory.",
    ),
)
_PRESET = {p.key: p for p in PRESETS}


@dataclass(frozen=True)
class AcmeProviderView:
    """A provider as the API shows it (never any secret)."""

    id: UUID
    name: str
    preset: str
    directory_url: str
    email: str | None
    auth_mode: str
    eab_kid: str | None
    eab_configured: bool
    oauth: dict[str, Any] | None
    oauth_secret_set: bool
    account_registered: bool
    ca_bundle_set: bool
    terms_agreed: bool
    enabled: bool
    default: bool
    used_by: int
    version: int


@dataclass(frozen=True)
class TestResult:
    """Outcome of testing a provider: the steps, then the verdict."""

    ok: bool
    detail: str
    stages: list[Stage]
    tos_url: str | None = None
    external_account_required: bool = False


def _view(row: AcmeProviderRow, used_by: int = 0) -> AcmeProviderView:
    return AcmeProviderView(
        id=row.id,
        name=row.name,
        preset=row.preset,
        directory_url=row.directory_url,
        email=row.email,
        auth_mode=row.auth_mode,
        eab_kid=row.eab_kid,
        eab_configured=bool(row.eab_kid and row.eab_hmac_ref),
        oauth=dict(row.oauth) if row.oauth else None,
        oauth_secret_set=bool(row.oauth_secret_ref),
        account_registered=row.account_registered,
        ca_bundle_set=bool(row.ca_bundle),
        terms_agreed=row.terms_agreed_at is not None,
        enabled=row.enabled,
        default=row.is_default,
        used_by=used_by,
        version=row.version,
    )


def check_account_key(pem: str) -> str | None:
    """Why ``pem`` cannot be an ACME account key, or ``None`` (RSA of 2048 bits or more)."""
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError):
        return "must be an unencrypted PEM private key"
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        return "must be an RSA key of at least 2048 bits"
    return None


def check_ca_bundle(pem: str) -> str | None:
    """Why ``pem`` is not a bundle of CA certificates, or ``None`` when it is."""
    try:
        certs = x509.load_pem_x509_certificates(pem.encode())
    except ValueError:
        return "must be PEM certificates"
    if not certs:
        return "must hold at least one certificate"
    if "PRIVATE KEY" in pem:
        return "must not contain a private key"
    return None


def bundle_file(pem: str) -> str:
    """A file holding ``pem`` (public certificates), named by its hash, for TLS verification."""
    digest = hashlib.sha256(pem.encode()).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f"switchyard-ca-{digest}.pem"
    if not path.exists():
        path.write_text(pem)
    return str(path)


def _secret_ref(name: str, what: str) -> str:
    return f"kv/system/acme/{name}/{what}"


class AcmeProviders:
    """Manage ACME providers and build issuers from them."""

    def __init__(
        self, db: Databases, secrets: Any, *, verify: bool = True, http_transport: Any = None
    ) -> None:
        """Create the service.

        Args:
            db: Database access.
            secrets: A backend that can both read and write (OpenBao).
            verify: TLS verification of ACME servers; ``False`` only for test servers.
            http_transport: Replaceable HTTP transport for OAuth calls (tests).
        """
        self._db = db
        self._secrets = secrets
        self._verify = verify
        self._transport = http_transport

    # -- reading ----------------------------------------------------------------------------------

    @staticmethod
    def presets() -> tuple[Preset, ...]:
        """The known certificate authorities."""
        return PRESETS

    async def list_providers(self) -> list[AcmeProviderView]:
        """Every provider, by name (any signed-in user may see them; no secrets)."""
        async with admin_session(self._db.admin) as session:
            rows = (
                (await session.execute(select(AcmeProviderRow).order_by(AcmeProviderRow.name)))
                .scalars()
                .all()
            )
            return [_view(r, await self._used_by(session, r.name)) for r in rows]

    @staticmethod
    async def _used_by(session: Any, name: str) -> int:
        count = await session.execute(
            select(func.count())
            .select_from(CertificateRow)
            .where(CertificateRow.acme_provider == name)
        )
        return int(count.scalar_one())

    async def get(self, provider_id: UUID) -> AcmeProviderView:
        """One provider."""
        row = await self._row(provider_id)
        async with admin_session(self._db.admin) as session:
            return _view(row, await self._used_by(session, row.name))

    async def _row(self, provider_id: UUID) -> AcmeProviderRow:
        async with admin_session(self._db.admin) as session:
            row = await session.get(AcmeProviderRow, provider_id)
        if row is None:
            raise NotFoundError(f"ACME provider {provider_id} does not exist")
        return row

    # -- writing ----------------------------------------------------------------------------------

    @staticmethod
    def _check(
        errors: FieldErrors,
        *,
        preset: str,
        directory_url: str | None,
        auth_mode: str,
        oauth: dict[str, Any] | None,
        has_secret: bool,
        eab_kid: str | None,
        has_eab_key: bool,
        email: str | None,
        account_key: str | None = None,
        ca_bundle: str | None = None,
        terms_agreed: bool = True,
    ) -> None:
        if not terms_agreed:
            errors.add("terms_agreed", "required", "accept the CA's terms of service")
        if account_key is not None and (problem := check_account_key(account_key)):
            errors.add("account_key", "invalid", problem)
        if ca_bundle and (problem := check_ca_bundle(ca_bundle)):
            errors.add("ca_bundle_pem", "invalid", problem)
        if preset not in _PRESET:
            errors.add("preset", "invalid", f"must be one of {', '.join(_PRESET)}")
        url = urlparse(directory_url or "")
        if url.scheme != "https" or not url.hostname or url.username or url.fragment:
            errors.add("directory_url", "invalid", "must be an https URL")
        if email is not None and not (3 <= len(email) <= 254 and "@" in email):
            errors.add("email", "invalid", "must be an e-mail address")
        if auth_mode not in AUTH_MODES:
            errors.add("auth_mode", "invalid", f"must be one of {', '.join(AUTH_MODES)}")
        if auth_mode == "eab" and not (eab_kid and has_eab_key):
            errors.add("eab_kid", "required", "External Account Binding needs a key id and key")
        if auth_mode == "oauth":
            data = oauth or {}
            for key in ("token_url", "client_id"):
                if not data.get(key):
                    errors.add(f"oauth.{key}", "required", "required for OAuth")
            if not has_secret:
                errors.add("oauth.client_secret", "required", "required for OAuth")
            for key in ("token_url", "eab_url"):
                if data.get(key) and urlparse(str(data[key])).scheme != "https":
                    errors.add(f"oauth.{key}", "invalid", "must be an https URL")
            if not data.get("eab_url") and not data.get("bearer_on_acme"):
                errors.add(
                    "oauth.eab_url",
                    "required",
                    "give an EAB endpoint or send the token on ACME requests",
                )

    @staticmethod
    def _oauth(raw: dict[str, Any] | None) -> dict[str, Any] | None:
        if raw is None:
            return None
        return {k: raw[k] for k in _OAUTH_KEYS if raw.get(k) not in (None, "")}

    async def create(
        self,
        principal: Principal,
        *,
        name: str,
        preset: str = "custom",
        directory_url: str | None = None,
        email: str | None = None,
        auth_mode: str | None = None,
        eab_kid: str | None = None,
        eab_hmac_key: str | None = None,
        oauth: dict[str, Any] | None = None,
        oauth_client_secret: str | None = None,
        account_key: str | None = None,
        ca_bundle_pem: str | None = None,
        terms_agreed: bool = False,
        enabled: bool = True,
        default: bool = False,
        request_id: str | None = None,
    ) -> AcmeProviderView:
        """Register a provider and create its account key (or store the one given).

        Raises:
            FieldErrors: With the path of every invalid field.
            AlreadyExistsError: If the name is taken.
        """
        require(principal, "manage", "acme_provider")
        errors = FieldErrors()
        try:
            v.validate_name(name)
        except ValueError as exc:
            errors.add("name", "invalid", str(exc))
        known = _PRESET.get(preset)
        url = directory_url or (known.directory_url if known else None)
        mode = auth_mode or (known.auth_mode if known else "none")
        oauth_clean = self._oauth(oauth)
        self._check(
            errors,
            preset=preset,
            directory_url=url,
            auth_mode=mode,
            oauth=oauth_clean,
            has_secret=bool(oauth_client_secret),
            eab_kid=eab_kid,
            has_eab_key=bool(eab_hmac_key),
            email=email,
            account_key=account_key,
            ca_bundle=ca_bundle_pem,
            terms_agreed=terms_agreed,
        )
        errors.raise_if_any()
        url = url or ""
        row = AcmeProviderRow(
            id=uuid7(),
            name=name,
            preset=preset,
            directory_url=url,
            email=email,
            auth_mode=mode,
            oauth=oauth_clean if mode == "oauth" else None,
            eab_kid=eab_kid if mode == "eab" else None,
            account_key_ref=_secret_ref(name, "account-key"),
            ca_bundle=ca_bundle_pem or None,
            terms_agreed_at=datetime.now(UTC),
            is_default=default,
            enabled=enabled,
        )
        try:
            await self._secrets.put(row.account_key_ref, account_key or new_account_key())
            if mode == "eab" and eab_hmac_key:
                row.eab_hmac_ref = _secret_ref(name, "eab-hmac")
                await self._secrets.put(row.eab_hmac_ref, eab_hmac_key)
            if mode == "oauth" and oauth_client_secret:
                row.oauth_secret_ref = _secret_ref(name, "oauth-client-secret")
                await self._secrets.put(row.oauth_secret_ref, oauth_client_secret)
        except SecretBackendError as exc:
            raise ConflictError(f"could not store the provider's secrets: {exc}") from exc
        async with admin_session(self._db.admin) as session:
            if (
                await session.execute(
                    select(AcmeProviderRow.id).where(AcmeProviderRow.name == name)
                )
            ).first():
                raise AlreadyExistsError(f"ACME provider {name} already exists")
            if default:
                await self._clear_default(session)
            session.add(row)
            await session.flush()
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="acme_provider.created",
                object_ref=f"acme_provider:{name}",
                diff={"preset": preset, "auth_mode": mode, "directory_url": url},
                request_id=request_id,
            )
            return _view(row)

    @staticmethod
    async def _clear_default(session: Any) -> None:
        for other in (
            (await session.execute(select(AcmeProviderRow).where(AcmeProviderRow.is_default)))
            .scalars()
            .all()
        ):
            other.is_default = False
        await session.flush()

    async def update(
        self,
        principal: Principal,
        provider_id: UUID,
        *,
        expected_version: int | None = None,
        directory_url: str | None = None,
        email: str | None = None,
        auth_mode: str | None = None,
        eab_kid: str | None = None,
        eab_hmac_key: str | None = None,
        oauth: dict[str, Any] | None = None,
        oauth_client_secret: str | None = None,
        account_key: str | None = None,
        ca_bundle_pem: str | None = None,
        enabled: bool | None = None,
        default: bool | None = None,
        request_id: str | None = None,
    ) -> AcmeProviderView:
        """Change a provider; omitted fields (and omitted secrets) stay as they are.

        A new ``account_key`` replaces the account (it has to be registered again). An empty
        ``ca_bundle_pem`` removes the extra trusted roots.


        Changing the directory or the authentication means the account must be registered again,
        so ``account_registered`` goes back to false.
        """
        require(principal, "manage", "acme_provider")
        current = await self._row(provider_id)
        mode = auth_mode or current.auth_mode
        merged_oauth = (
            {**(current.oauth or {}), **(self._oauth(oauth) or {})}
            if (oauth is not None or mode == "oauth")
            else None
        )
        errors = FieldErrors()
        self._check(
            errors,
            preset=current.preset,
            directory_url=directory_url or current.directory_url,
            auth_mode=mode,
            oauth=merged_oauth,
            has_secret=bool(oauth_client_secret or current.oauth_secret_ref),
            eab_kid=eab_kid or current.eab_kid,
            has_eab_key=bool(eab_hmac_key or current.eab_hmac_ref),
            email=email if email is not None else current.email,
            account_key=account_key,
            ca_bundle=ca_bundle_pem,
        )
        errors.raise_if_any()
        try:
            if account_key:
                await self._secrets.put(current.account_key_ref, account_key)
            if eab_hmac_key:
                await self._secrets.put(_secret_ref(current.name, "eab-hmac"), eab_hmac_key)
            if oauth_client_secret:
                await self._secrets.put(
                    _secret_ref(current.name, "oauth-client-secret"), oauth_client_secret
                )
        except SecretBackendError as exc:
            raise ConflictError(f"could not store the provider's secrets: {exc}") from exc
        async with admin_session(self._db.admin) as session:
            row = await session.get(AcmeProviderRow, provider_id, with_for_update=True)
            if row is None:
                raise NotFoundError(f"ACME provider {provider_id} does not exist")
            if expected_version is not None and row.version != expected_version:
                raise PreconditionFailedError(
                    f"ACME provider {row.name} is at version {row.version}, not {expected_version}"
                )
            changed: dict[str, Any] = {}
            if directory_url and directory_url != row.directory_url:
                row.directory_url, row.account_registered = directory_url, False
                changed["directory_url"] = directory_url
            if email is not None:
                row.email = email or None
            if account_key:
                row.account_registered = False
                changed["account_key"] = "replaced"
            if ca_bundle_pem is not None:
                row.ca_bundle = ca_bundle_pem or None
                changed["ca_bundle"] = bool(ca_bundle_pem)
            if mode != row.auth_mode:
                row.auth_mode, row.account_registered = mode, False
                changed["auth_mode"] = mode
            if mode == "eab":
                row.eab_kid = eab_kid or row.eab_kid
                if eab_hmac_key:
                    row.eab_hmac_ref = _secret_ref(row.name, "eab-hmac")
                    row.account_registered = False
                row.oauth, row.oauth_secret_ref = None, None
            elif mode == "oauth":
                row.oauth = merged_oauth
                if oauth_client_secret:
                    row.oauth_secret_ref = _secret_ref(row.name, "oauth-client-secret")
                row.eab_kid, row.eab_hmac_ref = None, None
            else:
                row.oauth = row.oauth_secret_ref = row.eab_kid = row.eab_hmac_ref = None
            if enabled is not None:
                row.enabled = enabled
            if default is not None and default != row.is_default:
                if default:
                    await self._clear_default(session)
                row.is_default = default
                changed["default"] = default
            row.version += 1
            await session.flush()
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="acme_provider.updated",
                object_ref=f"acme_provider:{row.name}",
                diff=changed,
                request_id=request_id,
            )
            return _view(row, await self._used_by(session, row.name))

    async def delete(
        self,
        principal: Principal,
        provider_id: UUID,
        *,
        expected_version: int | None = None,
        request_id: str | None = None,
    ) -> None:
        """Remove a provider that no certificate uses."""
        require(principal, "manage", "acme_provider")
        row = await self._row(provider_id)
        if expected_version is not None and row.version != expected_version:
            raise PreconditionFailedError(
                f"ACME provider {row.name} is at version {row.version}, not {expected_version}"
            )
        async with admin_session(self._db.admin) as session:
            used = (
                await session.execute(
                    select(func.count())
                    .select_from(CertificateRow)
                    .where(CertificateRow.acme_provider == row.name)
                )
            ).scalar_one()
            if used:
                raise ConflictError(
                    f"{used} certificate(s) use the ACME provider {row.name}; change them first"
                )
            await session.delete(await session.get(AcmeProviderRow, provider_id))
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="acme_provider.deleted",
                object_ref=f"acme_provider:{row.name}",
                diff={},
                request_id=request_id,
            )

    # -- using ------------------------------------------------------------------------------------

    async def _by_name(self, name: str | None) -> AcmeProviderRow:
        async with admin_session(self._db.admin) as session:
            query = select(AcmeProviderRow).where(AcmeProviderRow.enabled)
            query = (
                query.where(AcmeProviderRow.name == name)
                if name
                else query.where(AcmeProviderRow.is_default)
            )
            row = (await session.execute(query)).scalar_one_or_none()
        if row is None and name in (None, "default"):
            row = await self._import_environment()
        if row is None:
            raise AcmeError(
                f"ACME provider {name} does not exist or is disabled"
                if name
                else "no default ACME provider is set: choose one or set a default"
            )
        return row

    async def _import_environment(self) -> AcmeProviderRow | None:
        """Create the provider ``default`` from the variables of earlier versions, once.

        ``SWITCHYARD_ACME_DIRECTORY``, ``SWITCHYARD_ACME_EMAIL`` and
        ``SWITCHYARD_ACME_ACCOUNT_KEY_FILE`` described the only provider before providers were
        managed in the API; if they are set and no provider exists, they become one.
        """
        directory = os.environ.get("SWITCHYARD_ACME_DIRECTORY")
        key_file = os.environ.get("SWITCHYARD_ACME_ACCOUNT_KEY_FILE")
        if not directory or not key_file:
            return None
        async with admin_session(self._db.admin) as session:
            if (
                await session.execute(select(func.count()).select_from(AcmeProviderRow))
            ).scalar_one():
                return None
        path = Path(key_file)
        if not path.exists():
            path.write_text(new_account_key())
            path.chmod(0o600)
        ref = _secret_ref("default", "account-key")
        await self._secrets.put(ref, path.read_text())
        row = AcmeProviderRow(
            id=uuid7(),
            name="default",
            preset="custom",
            directory_url=directory,
            email=os.environ.get("SWITCHYARD_ACME_EMAIL"),
            auth_mode="none",
            account_key_ref=ref,
            terms_agreed_at=datetime.now(UTC),
            is_default=True,
        )
        async with admin_session(self._db.admin) as session:
            session.add(row)
            await audit_log.record(
                session,
                tenant_id=None,
                actor="system:certs",
                action="acme_provider.created",
                object_ref="acme_provider:default",
                diff={"imported_from": "environment", "directory_url": directory},
            )
        return row

    async def issuer(self, name: str | None, solver: Solver) -> AcmeIssuer:
        """Build an issuer for the named provider (the default one when ``name`` is empty).

        Raises:
            AcmeError: If the provider is missing, disabled or its secrets cannot be read.
        """
        return await self._issuer(await self._by_name(name), solver)

    async def _issuer(self, row: AcmeProviderRow, solver: Solver) -> AcmeIssuer:
        try:
            account_key = await self._secrets.get(row.account_key_ref)
            auth = AcmeAuth()
            if row.auth_mode == "eab":
                if not (row.eab_kid and row.eab_hmac_ref):
                    raise AcmeError("the EAB key id or key is missing")
                auth = AcmeAuth(eab=Eab(row.eab_kid, await self._secrets.get(row.eab_hmac_ref)))
            elif row.auth_mode == "oauth":
                if not (row.oauth and row.oauth_secret_ref):
                    raise AcmeError("the OAuth settings or client secret are missing")
                cfg = row.oauth
                auth = AcmeAuth(
                    oauth=ClientCredentials(
                        cfg["token_url"],
                        cfg["client_id"],
                        await self._secrets.get(row.oauth_secret_ref),
                        scope=cfg.get("scope"),
                        audience=cfg.get("audience"),
                        transport=self._transport,
                        verify=self._verify,
                    ),
                    bearer_on_acme=bool(cfg.get("bearer_on_acme")),
                    eab_url=cfg.get("eab_url"),
                )
        except Exception as exc:
            raise AcmeError(f"cannot read the secrets of ACME provider {row.name}: {exc}") from exc
        verify: bool | str = self._verify
        if row.ca_bundle:
            verify = bundle_file(row.ca_bundle)
        return AcmeIssuer(
            row.directory_url,
            account_key,
            solver,
            email=row.email,
            auth=auth,
            verify=verify,
            http_transport=self._transport,
        )

    async def test(
        self, principal: Principal, provider_id: UUID, *, request_id: str | None = None
    ) -> TestResult:
        """Register the account at the CA (or find it); record the result."""
        require(principal, "manage", "acme_provider")
        row = await self._row(provider_id)
        result: TestResult
        try:
            issuer = await self._issuer(row, _NoSolver())
            stages = await issuer.probe()
            ok = all(s.ok for s in stages)
            failed = next((s for s in stages if not s.ok), None)
            registration = await issuer.register() if ok else None
            result = TestResult(
                ok,
                stages[-1].detail if ok else f"{failed.name}: {failed.detail}" if failed else "",
                stages,
                registration.terms_of_service if registration else None,
                registration.external_account_required if registration else False,
            )
        except AcmeError as exc:
            result = TestResult(False, str(exc), [Stage("account", False, str(exc))])
        async with admin_session(self._db.admin) as session:
            stored = await session.get(AcmeProviderRow, provider_id)
            if stored is not None:
                stored.account_registered = result.ok
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="acme_provider.tested",
                object_ref=f"acme_provider:{row.name}",
                diff={"ok": result.ok},
                request_id=request_id,
            )
        return result


class _NoSolver:
    """A solver for account registration, which proves nothing."""

    challenge_type = challenges.DNS01

    async def present(self, domain: str, challenge: Any, key: Any) -> Any:  # pragma: no cover
        raise AcmeError("registration does not validate names")

    async def settle(self) -> None:  # pragma: no cover
        return None

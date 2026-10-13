# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""ACME client (Let's Encrypt, ZeroSSL, step-ca, Keyfactor ...) with pluggable challenge solvers.

A certificate is issued once and then distributed to every SE, never per SE (backend design). The
``acme`` library is synchronous, so each blocking step runs in a worker thread while the DNS and
engine calls stay asynchronous. Whatever a solver published is always removed again, also when
validation fails.

How the controller proves control of a name is a *solver*:

- :class:`Dns01Solver` publishes ``_acme-challenge`` TXT records through a DNS provider, chosen per
  name (wildcards and apex names need DNS-01).
- :class:`Http01Solver` hands the token to a publisher that makes the SEs answer
  ``/.well-known/acme-challenge/<token>`` on port 80.

How the controller authenticates to the CA is :class:`AcmeAuth`: nothing, an External Account
Binding (kid and HMAC key), or an OAuth bearer token (sent on every ACME request when the CA wants
it, and used to fetch a fresh EAB key from a management endpoint, as Keyfactor does).
"""

import asyncio
import base64
import datetime
import hashlib
import logging
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from acme import challenges, client, errors, messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from josepy.json_util import JSONObjectWithFields
from josepy.jwk import JWKRSA

from switchyard.adapters.dns.base import DnsError, DnsProvider
from switchyard.adapters.oauth import ClientCredentials, OAuthError

log = logging.getLogger("switchyard.acme")


class AcmeError(Exception):
    """Issuing a certificate through ACME failed."""


def new_account_key() -> str:
    """Create an RSA-2048 account key and return it as PKCS#8 PEM."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@dataclass(frozen=True)
class Eab:
    """External Account Binding credentials: the key id and the base64url HMAC key."""

    kid: str
    hmac_key: str


class Progress(Protocol):
    """Where an issuance reports its stages (a job's progress recorder)."""

    def stage(self, name: str) -> AbstractAsyncContextManager[None]:
        """Run a block as a named stage; an exception marks it failed and propagates."""
        ...

    async def note(self, message: str, level: str = "info") -> None:
        """Add a line to the log."""
        ...


class NoProgress:
    """A :class:`Progress` that records nothing."""

    @asynccontextmanager
    async def stage(self, name: str) -> AsyncIterator[None]:
        """Run the block."""
        yield

    async def note(self, message: str, level: str = "info") -> None:
        """Ignore the line."""


#: The stages of an ACME issuance, in order.
ISSUE_STAGES = ("order", "validate", "finalize")


class Solver(Protocol):
    """Proves control of a name for one challenge type."""

    typ: str

    def select(self, offered: list[Any]) -> Any | None:
        """The challenge of this type among those the CA offers, if there is one."""
        ...

    def answer(self, challenge: Any, key: JWKRSA) -> Any:
        """The response that tells the CA the proof is in place."""
        ...

    async def present(
        self, domain: str, challenge: Any, key: JWKRSA
    ) -> Callable[[], Awaitable[None]]:
        """Publish the proof; return the function that removes it again."""
        ...

    async def settle(self) -> None:
        """Wait until what was published can be seen by the CA."""
        ...


class Dns01Solver:
    """DNS-01: TXT records at ``_acme-challenge.<name>``, through the provider of each name."""

    typ = "dns-01"

    def select(self, offered: list[Any]) -> Any | None:
        """The DNS-01 challenge, if offered."""
        return next((c for c in offered if isinstance(c.chall, challenges.DNS01)), None)

    def answer(self, challenge: Any, key: JWKRSA) -> Any:
        """The key authorization response."""
        return challenge.response(key)

    def __init__(
        self,
        resolve: Callable[[str], Awaitable[DnsProvider]],
        *,
        wait_s: float = 15.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        """Create the solver.

        Args:
            resolve: Finds the DNS provider for a name (raises ``DnsError`` when there is none).
            wait_s: Pause after publishing so resolvers see the records.
            sleep: Awaitable sleep, replaceable in tests.
        """
        self._resolve = resolve
        self._wait = wait_s
        self._sleep = sleep

    async def present(
        self, domain: str, challenge: Any, key: JWKRSA
    ) -> Callable[[], Awaitable[None]]:
        """Create the TXT record."""
        name = challenge.chall.validation_domain_name(domain)
        provider = await self._resolve(domain)
        handle = await provider.create_txt(name, challenge.chall.validation(key))

        async def remove() -> None:
            try:
                await provider.delete_txt(name, handle)
            except DnsError as exc:
                log.warning("could not remove TXT record %s: %s", name, exc)

        return remove

    async def settle(self) -> None:
        """Pause so the records propagate."""
        if self._wait:
            await self._sleep(self._wait)


class HttpPublisher(Protocol):
    """Makes the engines answer an HTTP-01 challenge."""

    async def publish(self, domain: str, path: str, body: str) -> None:
        """Serve ``body`` at ``http://<domain><path>``."""
        ...

    async def withdraw(self, domain: str, path: str) -> None:
        """Stop serving it."""
        ...

    async def close(self) -> None:
        """Remove anything still published and release connections."""
        ...


class Http01Solver:
    """HTTP-01: the engines answer ``/.well-known/acme-challenge/<token>`` on port 80."""

    typ = "http-01"

    def select(self, offered: list[Any]) -> Any | None:
        """The HTTP-01 challenge, if offered."""
        return next((c for c in offered if isinstance(c.chall, challenges.HTTP01)), None)

    def answer(self, challenge: Any, key: JWKRSA) -> Any:
        """The key authorization response."""
        return challenge.response(key)

    def __init__(self, publisher: HttpPublisher) -> None:
        """Create the solver around the publisher that talks to the engines."""
        self._publisher = publisher

    async def present(
        self, domain: str, challenge: Any, key: JWKRSA
    ) -> Callable[[], Awaitable[None]]:
        """Publish the key authorization."""
        path = challenge.chall.path
        await self._publisher.publish(domain, path, challenge.chall.validation(key))

        async def remove() -> None:
            try:
                await self._publisher.withdraw(domain, path)
            except Exception as exc:
                log.warning("could not withdraw the HTTP-01 challenge for %s: %s", domain, exc)

        return remove

    async def settle(self) -> None:
        """Nothing to wait for: the publisher returns when the engines serve the answer."""


class TlsAlpnPublisher(Protocol):
    """Makes the engines present a challenge certificate to ``acme-tls/1`` handshakes."""

    async def publish(self, domain: str, certificate_pem: str) -> None:
        """Present ``certificate_pem`` (certificate and key) for ``domain``."""
        ...

    async def withdraw(self, domain: str) -> None:
        """Stop presenting it."""
        ...

    async def close(self) -> None:
        """Remove anything still published and release connections."""
        ...


#: The ``id-pe-acmeIdentifier`` extension of RFC 8737.
ACME_IDENTIFIER_OID = "1.3.6.1.5.5.7.1.31"


def challenge_certificate(domain: str, key_authorization: str) -> str:
    """The self-signed certificate RFC 8737 asks a server to present for ``domain``.

    It names only ``domain`` and carries the SHA-256 of the key authorization in a critical
    ``acmeIdentifier`` extension. Returned as PEM: certificate, then its (throwaway) key.

    Example:
        >>> pem = challenge_certificate("a.example.test", "token.thumbprint")
        >>> pem.count("BEGIN CERTIFICATE"), "PRIVATE KEY" in pem
        (1, True)
    """
    key = ec.generate_private_key(ec.SECP256R1())
    digest = hashlib.sha256(key_authorization.encode()).digest()
    extension = x509.UnrecognizedExtension(
        x509.ObjectIdentifier(ACME_IDENTIFIER_OID), b"\x04\x20" + digest
    )
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
        .add_extension(extension, critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode()
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
    )


class _EmptyResponse(JSONObjectWithFields):
    """The answer to a tls-alpn-01 challenge: an empty JSON object (RFC 8555, 7.5.1)."""

    def __bool__(self) -> bool:
        """True, so the library sends ``{}`` and not an empty body (a POST-as-GET)."""
        return True

    def to_partial_json(self) -> dict[str, Any]:
        """Nothing to send but the request itself."""
        return {}


class TlsAlpn01Solver:
    """TLS-ALPN-01: the engines present a challenge certificate to ``acme-tls/1`` handshakes."""

    typ = "tls-alpn-01"

    def __init__(self, publisher: TlsAlpnPublisher) -> None:
        """Create the solver around the publisher that talks to the engines."""
        self._publisher = publisher

    def select(self, offered: list[Any]) -> Any | None:
        """The tls-alpn-01 challenge, if offered (the library does not know the type)."""
        return next(
            (
                c
                for c in offered
                if isinstance(c.chall, challenges.UnrecognizedChallenge)
                and c.chall.jobj.get("type") == self.typ
            ),
            None,
        )

    def answer(self, challenge: Any, key: JWKRSA) -> Any:
        """An empty response."""
        return _EmptyResponse()

    async def present(
        self, domain: str, challenge: Any, key: JWKRSA
    ) -> Callable[[], Awaitable[None]]:
        """Build the challenge certificate and hand it to the engines."""
        token = str(challenge.chall.jobj["token"])
        thumbprint = base64.urlsafe_b64encode(key.thumbprint()).rstrip(b"=").decode()
        key_authorization = f"{token}.{thumbprint}"
        await self._publisher.publish(domain, challenge_certificate(domain, key_authorization))

        async def remove() -> None:
            try:
                await self._publisher.withdraw(domain)
            except Exception as exc:
                log.warning(
                    "could not withdraw the TLS-ALPN-01 certificate for %s: %s", domain, exc
                )

        return remove

    async def settle(self) -> None:
        """Nothing to wait for: the engines present it as soon as the publisher returns."""


@dataclass(frozen=True)
class AcmeAuth:
    """How the controller authenticates to the CA.

    Attributes:
        eab: Static External Account Binding credentials.
        oauth: Token source for CAs behind an identity provider.
        bearer_on_acme: Send the bearer token on every ACME request.
        eab_url: Management endpoint that returns a fresh EAB key (needs ``oauth``). A GET with the
            bearer token must answer JSON with a key id and an HMAC key.
        eab_fields: Names of the key id and HMAC fields in that answer.
    """

    eab: Eab | None = None
    oauth: ClientCredentials | None = None
    bearer_on_acme: bool = False
    eab_url: str | None = None
    eab_fields: tuple[tuple[str, ...], tuple[str, ...]] = (
        ("kid", "keyId", "key_id", "id"),
        ("hmacKey", "hmac_key", "hmac", "key", "b64MacKey", "macKey"),
    )


class _BearerNetwork(client.ClientNetwork):
    """ACME network layer that adds a bearer token to every request."""

    def __init__(self, *args: Any, bearer: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._bearer = bearer

    def _send_request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        if self._bearer:
            kwargs.setdefault("headers", {})["Authorization"] = f"Bearer {self._bearer}"
        return super()._send_request(method, url, *args, **kwargs)


def _httpx_verify(verify: bool | str) -> bool | ssl.SSLContext:
    if isinstance(verify, str):
        return ssl.create_default_context(cafile=verify)
    return verify


@dataclass(frozen=True)
class Stage:
    """One step of a connection test.

    Attributes:
        name: ``directory``, ``eab`` or ``account``.
        ok: Whether it worked.
        detail: What was found, or why it failed.
    """

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Registration:
    """The result of registering (or finding) the ACME account."""

    uri: str
    terms_of_service: str | None
    external_account_required: bool


class AcmeIssuer:
    """Issues certificates from one ACME directory."""

    def __init__(
        self,
        directory_url: str,
        account_key_pem: str,
        solver: Solver,
        *,
        email: str | None = None,
        auth: AcmeAuth | None = None,
        verify: bool | str = True,
        timeout_s: float = 180.0,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create an issuer.

        Args:
            directory_url: ACME directory, for example Let's Encrypt's.
            account_key_pem: PKCS#8 PEM of the account key (create one with
                :func:`new_account_key` and keep it in the secrets backend).
            solver: How control of a name is proven.
            email: Contact address for the ACME account.
            auth: How to authenticate to the CA (EAB, OAuth); none when empty.
            verify: TLS verification of the ACME server: ``True``, ``False`` (test servers only)
                or the path of a PEM file with the CA certificates to trust (private CAs).
            timeout_s: How long to wait for the CA to validate and issue.
            http_transport: Replaceable transport for the EAB fetch (tests).
        """
        self._url = directory_url
        self._key = JWKRSA(
            key=serialization.load_pem_private_key(account_key_pem.encode(), password=None)
        )
        self._solver = solver
        self._email = email
        self._auth = auth or AcmeAuth()
        self._verify = verify
        self._verify_ssl: Any = verify  # the acme library passes it on to requests
        self._timeout = timeout_s
        self._transport = http_transport

    async def _bearer(self) -> str | None:
        oauth = self._auth.oauth
        if oauth is None:
            return None
        try:
            return await oauth.token()
        except OAuthError as exc:
            raise AcmeError(f"could not get an access token: {exc}") from exc

    async def _eab(self, bearer: str | None) -> Eab | None:
        auth = self._auth
        if auth.eab_url:
            if bearer is None:
                raise AcmeError("fetching an EAB key needs OAuth")
            return await self._fetch_eab(auth.eab_url, bearer)
        return auth.eab

    async def _fetch_eab(self, url: str, bearer: str) -> Eab:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, verify=_httpx_verify(self._verify), timeout=30.0
            ) as http:
                response = await http.get(url, headers={"Authorization": f"Bearer {bearer}"})
        except httpx.HTTPError as exc:
            raise AcmeError(f"fetching the EAB key from {url}: {exc}") from exc
        if response.status_code != 200:
            raise AcmeError(f"fetching the EAB key from {url}: HTTP {response.status_code}")
        body = response.json()
        if isinstance(body, list) and body:
            body = body[0]
        kid_names, hmac_names = self._auth.eab_fields
        kid = next(
            (str(body[n]) for n in kid_names if isinstance(body, dict) and body.get(n)), None
        )
        hmac_key = next(
            (str(body[n]) for n in hmac_names if isinstance(body, dict) and body.get(n)), None
        )
        if not kid or not hmac_key:
            raise AcmeError(f"the EAB answer from {url} has no key id and HMAC key")
        return Eab(kid, hmac_key)

    def _account(self, bearer: str | None, eab: Eab | None) -> tuple[client.ClientV2, Registration]:
        net = _BearerNetwork(
            self._key,
            account=None,
            verify_ssl=self._verify_ssl,
            bearer=bearer if self._auth.bearer_on_acme else None,
        )
        directory = client.ClientV2.get_directory(self._url, net)
        acme = client.ClientV2(directory, net)
        binding = None
        if eab is not None:
            binding = messages.ExternalAccountBinding.from_data(
                self._key.public_key(), eab.kid, eab.hmac_key, directory
            )
        elif acme.external_account_required():
            raise AcmeError("this CA requires External Account Binding credentials")
        registration = messages.NewRegistration.from_data(
            email=self._email, terms_of_service_agreed=True, external_account_binding=binding
        )
        try:
            resource = acme.new_account(registration)
            uri = str(resource.uri)
        except errors.ConflictError as exc:  # the account already exists: reuse it
            uri = str(exc.location)
            net.account = messages.RegistrationResource(uri=uri, body=registration)
        meta = getattr(directory, "meta", None)
        return acme, Registration(
            uri,
            getattr(meta, "terms_of_service", None),
            bool(getattr(meta, "external_account_required", False)),
        )

    @staticmethod
    async def _call(function: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking ACME call in a thread; retry when the CA rejects the anti-replay nonce.

        Servers may answer ``badNonce`` at any time; the protocol says to retry with a fresh one.
        """
        for attempt in range(3):
            try:
                return await asyncio.to_thread(function, *args)
            except (errors.BadNonce, messages.Error) as exc:
                bad_nonce = isinstance(exc, errors.BadNonce) or exc.code == "badNonce"
                if not bad_nonce or attempt == 2:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def probe(self) -> list[Stage]:
        """Test the connection step by step: the directory, the EAB credentials, the account.

        Nothing is ordered. The list stops at the first step that fails.
        """
        stages: list[Stage] = []
        try:
            net = client.ClientNetwork(self._key, account=None, verify_ssl=self._verify_ssl)
            directory = await self._call(client.ClientV2.get_directory, self._url, net)
            required = bool(
                getattr(getattr(directory, "meta", None), "external_account_required", False)
            )
            stages.append(
                Stage(
                    "directory",
                    True,
                    "external account binding required" if required else "reachable",
                )
            )
        except (errors.Error, OSError, ValueError) as exc:
            return [Stage("directory", False, str(exc))]
        try:
            bearer = await self._bearer()
            eab = await self._eab(bearer)
        except AcmeError as exc:
            return [*stages, Stage("eab", False, str(exc))]
        if eab is not None:
            stages.append(Stage("eab", True, f"key {eab.kid}"))
        elif required:
            return [
                *stages,
                Stage("eab", False, "the CA requires External Account Binding credentials"),
            ]
        try:
            _, registration = await self._call(self._account, bearer, eab)
        except (errors.Error, AcmeError, OSError, ValueError) as exc:
            return [*stages, Stage("account", False, str(exc))]
        return [*stages, Stage("account", True, f"account {registration.uri}")]

    async def register(self) -> Registration:
        """Register the account (or find it) without ordering anything.

        Raises:
            AcmeError: If the CA refuses it or authentication fails.
        """
        try:
            bearer = await self._bearer()
            eab = await self._eab(bearer)
            _, registration = await self._call(self._account, bearer, eab)
            return registration  # type: ignore[no-any-return]
        except (errors.Error, OSError, ValueError) as exc:
            raise AcmeError(f"ACME registration failed: {exc}") from exc

    async def issue(self, csr_pem: str, progress: Progress | None = None) -> str:
        """Order, validate with the solver and finalize. Returns the full chain as PEM.

        The stages reported to ``progress`` are ``order``, ``validate`` and ``finalize``. What the
        solver published is removed as soon as the CA has validated the names, and always when
        something fails.

        Raises:
            AcmeError: On any failure; whatever the solver published is removed first.
        """
        report = progress or NoProgress()
        cleanups: list[Callable[[], Awaitable[None]]] = []

        async def clean() -> None:
            if not cleanups:
                return
            while cleanups:
                await cleanups.pop()()
            await report.note("challenge records removed")

        try:
            async with report.stage("order"):
                bearer = await self._bearer()
                eab = await self._eab(bearer)
                acme, _ = await self._call(self._account, bearer, eab)
                order = await self._call(acme.new_order, csr_pem.encode())
            async with report.stage("validate"):
                pending: list[Any] = []
                for authz in order.authorizations:
                    if authz.body.status == messages.STATUS_VALID:
                        continue  # the CA remembers an earlier validation of this name
                    challenge = self._solver.select(list(authz.body.challenges))
                    if challenge is None:
                        raise AcmeError(f"the CA offers no {self._solver.typ} challenge")
                    domain = authz.body.identifier.value
                    cleanups.append(await self._solver.present(domain, challenge, self._key))
                    pending.append(challenge)
                await self._solver.settle()
                for challenge in pending:
                    await self._call(
                        acme.answer_challenge, challenge, self._solver.answer(challenge, self._key)
                    )
                deadline = datetime.datetime.now() + datetime.timedelta(seconds=self._timeout)
                order = await self._call(acme.poll_authorizations, order, deadline)
                await clean()
            async with report.stage("finalize"):
                deadline = datetime.datetime.now() + datetime.timedelta(seconds=self._timeout)
                done = await self._call(acme.finalize_order, order, deadline)
            return str(done.fullchain_pem)
        except (errors.Error, DnsError, OSError, ValueError) as exc:
            raise AcmeError(f"ACME issuance failed: {exc}") from exc
        finally:
            await clean()

"""ACME client (Let's Encrypt, step-ca) with DNS-01 validation through the Cloudflare adapter.

A certificate is issued once and then distributed to every SE, never per SE (backend design). The
``acme`` library is synchronous, so each blocking step runs in a worker thread while the DNS calls
stay asynchronous. TXT records are always removed again, also when validation fails.
"""

import asyncio
import datetime
import logging
from collections.abc import Callable
from typing import Any

from acme import challenges, client, errors, messages
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from josepy.jwk import JWKRSA

from switchyard.adapters.cloudflare import Cloudflare, CloudflareError

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


class AcmeIssuer:
    """Issues certificates from one ACME directory."""

    def __init__(
        self,
        directory_url: str,
        account_key_pem: str,
        dns: Cloudflare,
        *,
        email: str | None = None,
        verify: bool = True,
        dns_wait_s: float = 15.0,
        timeout_s: float = 180.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        """Create an issuer.

        Args:
            directory_url: ACME directory, for example Let's Encrypt's.
            account_key_pem: PKCS#8 PEM of the account key (create one with
                :func:`new_account_key` and keep it in the secrets backend).
            dns: Where DNS-01 TXT records are published.
            email: Contact address for the ACME account.
            verify: TLS verification of the ACME server (``False`` only for test servers).
            dns_wait_s: Pause after publishing TXT records so resolvers see them.
            timeout_s: How long to wait for the CA to validate and issue.
            sleep: Awaitable sleep, replaceable in tests.
        """
        self._url = directory_url
        self._key = JWKRSA(
            key=serialization.load_pem_private_key(account_key_pem.encode(), password=None)
        )
        self._dns = dns
        self._email = email
        self._verify = verify
        self._dns_wait = dns_wait_s
        self._timeout = timeout_s
        self._sleep = sleep

    def _account(self) -> client.ClientV2:
        net = client.ClientNetwork(self._key, account=None, verify_ssl=self._verify)
        acme = client.ClientV2(client.ClientV2.get_directory(self._url, net), net)
        registration = messages.NewRegistration.from_data(
            email=self._email, terms_of_service_agreed=True
        )
        try:
            acme.new_account(registration)
        except errors.ConflictError as exc:  # the account already exists: reuse it
            net.account = messages.RegistrationResource(uri=exc.location, body=registration)
        return acme

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

    async def issue(self, csr_pem: str) -> str:
        """Order, validate by DNS-01 and finalize. Returns the full chain as PEM.

        Raises:
            AcmeError: On any failure; created TXT records are removed first.
        """
        records: list[tuple[str, str]] = []
        try:
            acme = await self._call(self._account)
            order = await self._call(acme.new_order, csr_pem.encode())
            pending: list[Any] = []
            for authz in order.authorizations:
                if authz.body.status == messages.STATUS_VALID:
                    continue  # the CA remembers an earlier validation of this name
                challenge = next(
                    (c for c in authz.body.challenges if isinstance(c.chall, challenges.DNS01)),
                    None,
                )
                if challenge is None:
                    raise AcmeError("the CA offers no DNS-01 challenge")
                name = challenge.chall.validation_domain_name(authz.body.identifier.value)
                value = challenge.chall.validation(self._key)
                records.append((name, await self._dns.create_txt(name, value)))
                pending.append(challenge)
            if self._dns_wait:
                await self._sleep(self._dns_wait)
            for challenge in pending:
                await self._call(acme.answer_challenge, challenge, challenge.response(self._key))
            deadline = datetime.datetime.now() + datetime.timedelta(seconds=self._timeout)
            done = await self._call(acme.poll_and_finalize, order, deadline)
            return str(done.fullchain_pem)
        except (errors.Error, CloudflareError, OSError, ValueError) as exc:
            raise AcmeError(f"ACME issuance failed: {exc}") from exc
        finally:
            for name, record_id in records:
                try:
                    await self._dns.delete_txt(name, record_id)
                except CloudflareError as exc:
                    log.warning("could not remove TXT record %s: %s", name, exc)

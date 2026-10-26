# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""ACME authentication against Pebble: External Account Binding, OAuth-fetched EAB, bearer tokens."""

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from testcontainers.core.container import DockerContainer

from switchyard.adapters.acme import (
    AcmeAuth,
    AcmeError,
    AcmeIssuer,
    Dns01Solver,
    Eab,
    _BearerNetwork,
    new_account_key,
)
from switchyard.adapters.cloudflare import FakeCloudflare
from switchyard.adapters.oauth import ClientCredentials, OAuthError
from switchyard.services.certs import generate_key_and_csr

KID = "kid-1"
HMAC = "zWNDZM6eQGHWpSRTPal5eIUYFTu7EajVIoguysqZ9wG44nMEtx3MUAsUDkMTQ12W"
CONFIG = {
    "pebble": {
        "listenAddress": "0.0.0.0:14000",
        "managementListenAddress": "0.0.0.0:15000",
        "certificate": "test/certs/localhost/cert.pem",
        "privateKey": "test/certs/localhost/key.pem",
        "httpPort": 5002,
        "tlsPort": 5001,
        "ocspResponderURL": "",
        "externalAccountBindingRequired": True,
        "externalAccountMACKeys": {KID: HMAC},
    }
}


@pytest.fixture(scope="module")
def directory_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Pebble that insists on External Account Binding."""
    config = tmp_path_factory.mktemp("pebble") / "pebble-config.json"
    config.write_text(json.dumps(CONFIG))
    container = (
        DockerContainer("ghcr.io/letsencrypt/pebble:latest")
        .with_env("PEBBLE_VA_ALWAYS_VALID", "1")
        .with_env("PEBBLE_VA_NOSLEEP", "1")
        .with_volume_mapping(str(config), "/test/config/pebble-config.json", "ro")
        .with_exposed_ports(14000)
    )
    with container:
        url = f"https://{container.get_container_host_ip()}:{container.get_exposed_port(14000)}/dir"
        for _ in range(60):
            try:
                if httpx.get(url, verify=False, timeout=2).status_code == 200:  # noqa: S501
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        yield url


def solver() -> Dns01Solver:
    dns = FakeCloudflare()

    async def resolve(domain: str) -> FakeCloudflare:
        return dns

    return Dns01Solver(resolve, wait_s=0)


def csr() -> str:
    return generate_key_and_csr(("eab.example.test",))[1]


async def test_a_ca_that_requires_eab_refuses_an_account_without_it(directory_url: str) -> None:
    issuer = AcmeIssuer(directory_url, new_account_key(), solver(), verify=False)
    with pytest.raises(AcmeError, match="External Account Binding"):
        await issuer.issue(csr())


async def test_static_eab_credentials_register_the_account(directory_url: str) -> None:
    issuer = AcmeIssuer(
        directory_url,
        new_account_key(),
        solver(),
        verify=False,
        auth=AcmeAuth(eab=Eab(KID, HMAC)),
    )
    registration = await issuer.register()
    assert registration.external_account_required
    assert "BEGIN CERTIFICATE" in await issuer.issue(csr())


async def test_wrong_eab_key_is_refused(directory_url: str) -> None:
    issuer = AcmeIssuer(
        directory_url,
        new_account_key(),
        solver(),
        verify=False,
        auth=AcmeAuth(eab=Eab(KID, "d3Jvbmcta2V5LXdyb25nLWtleS13cm9uZy1rZXk")),
    )
    with pytest.raises(AcmeError):
        await issuer.register()


class Idp:
    """A token endpoint and an EAB endpoint, as a CA behind OAuth would have."""

    def __init__(self, *, kid: str = KID) -> None:
        self.kid = kid
        self.requests: list[tuple[str, str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            (request.method, request.url.path, request.headers.get("authorization"))
        )
        if request.url.path == "/token":
            form = dict(p.split("=", 1) for p in request.content.decode().split("&"))
            if form.get("client_secret") != "right":
                return httpx.Response(401, json={"error": "invalid_client"})
            assert form["grant_type"] == "client_credentials"
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        if request.url.path == "/ACME/KeyManagement":
            if request.headers.get("authorization") != "Bearer tok-1":
                return httpx.Response(401)
            return httpx.Response(200, json={"keyId": self.kid, "hmacKey": HMAC})
        return httpx.Response(404)


def oauth(idp: Idp, secret: str = "right") -> ClientCredentials:  # noqa: S107
    return ClientCredentials(
        "https://idp.example.test/token",
        "switchyard",
        secret,
        scope="acme",
        transport=httpx.MockTransport(idp.handler),
    )


async def test_an_eab_key_is_fetched_with_the_bearer_token(directory_url: str) -> None:
    idp = Idp()
    issuer = AcmeIssuer(
        directory_url,
        new_account_key(),
        solver(),
        verify=False,
        auth=AcmeAuth(oauth=oauth(idp), eab_url="https://kf.example.test/ACME/KeyManagement"),
        http_transport=httpx.MockTransport(idp.handler),
    )
    assert "BEGIN CERTIFICATE" in await issuer.issue(csr())
    assert ("GET", "/ACME/KeyManagement", "Bearer tok-1") in idp.requests


async def test_a_refused_client_secret_is_reported(directory_url: str) -> None:
    idp = Idp()
    issuer = AcmeIssuer(
        directory_url,
        new_account_key(),
        solver(),
        verify=False,
        auth=AcmeAuth(oauth=oauth(idp, "wrong"), eab_url="https://kf.example.test/x"),
        http_transport=httpx.MockTransport(idp.handler),
    )
    with pytest.raises(AcmeError, match="access token"):
        await issuer.register()


async def test_an_eab_answer_without_a_key_is_reported(directory_url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    issuer = AcmeIssuer(
        directory_url,
        new_account_key(),
        solver(),
        verify=False,
        auth=AcmeAuth(oauth=oauth(Idp()), eab_url="https://kf.example.test/ACME/KeyManagement"),
        http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AcmeError, match="no key id and HMAC key"):
        await issuer.register()


async def test_oauth_tokens_are_cached_until_they_expire() -> None:
    idp = Idp()
    source = oauth(idp)
    assert await source.token() == await source.token() == "tok-1"
    assert [r for r in idp.requests if r[1] == "/token"] == [("POST", "/token", None)]
    bad = oauth(idp, "wrong")
    with pytest.raises(OAuthError, match="HTTP 401"):
        await bad.token()


def test_the_bearer_token_goes_on_every_acme_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_send(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> str:
        seen.append(kwargs.get("headers", {}))
        return "ok"

    monkeypatch.setattr("acme.client.ClientNetwork._send_request", fake_send)
    network = _BearerNetwork(
        _jwk(),
        account=None,
        verify_ssl=False,
        bearer="tok-9",
    )
    network._send_request("GET", "https://ca.example.test/dir")
    assert seen == [{"Authorization": "Bearer tok-9"}]
    _BearerNetwork(_jwk(), account=None, verify_ssl=False)._send_request("GET", "https://x.test")
    assert seen[-1] == {}


def _jwk() -> Any:
    from cryptography.hazmat.primitives import serialization
    from josepy.jwk import JWKRSA

    return JWKRSA(key=serialization.load_pem_private_key(new_account_key().encode(), password=None))

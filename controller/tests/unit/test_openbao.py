# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""OpenBao KV v2 client against a mocked server."""

import json

import httpx
import pytest

from switchyard.adapters.secrets import OpenBaoSecrets, SecretBackendError, SecretNotFoundError


class Bao:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.logins = 0
        self.valid_token = ""
        self.expire_next = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/auth/approle/login":
            body = json.loads(request.content)
            if body["secret_id"] != "good":
                return httpx.Response(400, json={"errors": ["bad"]})
            self.logins += 1
            self.valid_token = f"tok-{self.logins}"
            return httpx.Response(200, json={"auth": {"client_token": self.valid_token}})
        if request.headers.get("x-vault-token") != self.valid_token or self.expire_next:
            self.expire_next = False
            self.valid_token = "rotated"
            return httpx.Response(403, json={"errors": ["permission denied"]})
        key = path.removeprefix("/v1/")
        if request.method == "POST":
            self.store[key] = json.loads(request.content)["data"]["value"]
            return httpx.Response(200, json={"data": {"version": 1}})
        if key in self.store:
            return httpx.Response(200, json={"data": {"data": {"value": self.store[key]}}})
        return httpx.Response(404, json={"errors": []})


def client(bao: Bao, secret_id: str = "good") -> OpenBaoSecrets:  # noqa: S107
    return OpenBaoSecrets(
        "https://bao:8200", "role", secret_id, transport=httpx.MockTransport(bao.handler)
    )


async def test_put_then_get_round_trips_through_kv_v2_paths() -> None:
    bao = Bao()
    s = client(bao)
    await s.put("kv/tenants/acme/certs/web", "PEM")
    assert bao.store == {"kv/data/tenants/acme/certs/web": "PEM"}
    assert await s.get("kv/tenants/acme/certs/web") == "PEM"
    assert bao.logins == 1


async def test_missing_secret_and_bad_references() -> None:
    s = client(Bao())
    with pytest.raises(SecretNotFoundError):
        await s.get("kv/nothing/here")
    with pytest.raises(SecretNotFoundError):
        await s.get("noslash")


async def test_expired_token_triggers_one_new_login() -> None:
    bao = Bao()
    s = client(bao)
    await s.put("kv/a/b", "1")
    bao.expire_next = True
    assert await s.get("kv/a/b") == "1"
    assert bao.logins == 2


async def test_failed_login_is_a_backend_error() -> None:
    with pytest.raises(SecretBackendError, match="login failed"):
        await client(Bao(), secret_id="bad").get("kv/a/b")


async def test_pki_signing_goes_through_the_role_endpoint() -> None:
    from datetime import timedelta

    from switchyard.adapters.pki import OpenBaoPki, PkiError
    from switchyard.services.certs import generate_key_and_csr

    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/approle/login":
            return httpx.Response(200, json={"auth": {"client_token": "t"}})
        assert request.url.path == "/v1/pki/sign/agents"
        seen.append(json.loads(request.content))
        if len(seen) == 2:
            return httpx.Response(200, json={"data": {}})
        data = {"certificate": "CERT", "issuing_ca": "CA", "serial_number": "AA:BB:cc"}
        return httpx.Response(200, json={"data": data})

    bao = OpenBaoSecrets("https://bao:8200", "r", "good", transport=httpx.MockTransport(handler))
    pki = OpenBaoPki(bao)
    csr = generate_key_and_csr(("x.example.test",))[1]
    signed = await pki.sign(csr, common_name="se-a", ip_sans=("10.0.0.5",), ttl=timedelta(days=7))
    assert (signed.cert_pem, signed.ca_pem, signed.serial) == ("CERT", "CA", "aabbcc")
    assert seen[0]["ip_sans"] == "10.0.0.5" and seen[0]["ttl"] == "604800s"
    assert seen[0]["common_name"] == "se-a"
    with pytest.raises(PkiError, match="no certificate"):
        await pki.sign(csr, common_name="se-a", ip_sans=(), ttl=timedelta(days=1))

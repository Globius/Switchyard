# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Regression coverage for tenant-selected endpoints and bounded JWKS downloads."""

import asyncio
import json
import socket
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from switchyard.adapters.dns.base import resolve_endpoint
from switchyard.adapters.dns.powerdns import PowerDns
from switchyard.adapters.dns.rfc2136 import Rfc2136
from switchyard.adapters.endpoint import pin_request
from switchyard.adapters.kubernetes import HttpKube
from switchyard.services.jwks import JwksError
from switchyard.services.jwks_sync import MAX_BYTES, http_fetch


def answers(*addresses: str) -> list[tuple[Any, ...]]:
    """Return getaddrinfo answers without contacting DNS."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]


@pytest.mark.parametrize("consumer", ["kubernetes", "powerdns", "jwks"])
async def test_clients_connect_to_the_checked_address_not_rebound_dns(
    consumer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWITCHYARD_ENGINE_POLICY", "strict")
    lookups: list[str] = []
    sent: list[httpx.Request] = []

    def resolve(host: str, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        lookups.append(host)
        return answers("8.8.8.8" if len(lookups) == 1 else "169.254.169.254")

    async def send(self: Any, request: httpx.Request) -> httpx.Response:
        sent.append(request)
        content = json.dumps({"gitVersion": "v1.31.0", "keys": [], "rrsets": []}).encode()
        return httpx.Response(200, stream=httpx.ByteStream(content))

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    url = "https://tenant.example.test:6443"
    if consumer == "kubernetes":
        kube = HttpKube(url, "test-token")
        try:
            assert await kube.version() == "v1.31.0"
        finally:
            await kube.close()
    elif consumer == "powerdns":
        dns = PowerDns(url, "test-key", "example.test")
        try:
            assert await dns._values("_acme-challenge.example.test") == (60, [])
        finally:
            await dns.close()
    else:
        assert '"keys"' in await http_fetch(url + "/jwks.json")
    assert lookups == ["tenant.example.test"]
    assert len(sent) == 1
    assert sent[0].url.host == "8.8.8.8"
    assert sent[0].url.port == 6443
    assert sent[0].headers["host"] == "tenant.example.test:6443"
    assert sent[0].extensions["sni_hostname"] == "tenant.example.test"


async def test_mixed_dns_answers_are_refused_before_sending_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWITCHYARD_ENGINE_POLICY", "strict")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: answers("8.8.8.8", "127.0.0.1"))
    request = httpx.Request("GET", "https://tenant.example.test", headers={"Authorization": "t"})
    with pytest.raises(httpx.ConnectError, match="may contact"):
        await pin_request(request, what="api_url", env="SWITCHYARD_K8S_CIDRS")
    assert request.url.host == "tenant.example.test"


def test_empty_dns_answers_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [])
    with pytest.raises(ValueError, match="cannot resolve"):
        resolve_endpoint("tenant.example.test", "server")


async def test_rfc2136_connects_to_an_allowed_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    import dns.message
    import dns.query

    monkeypatch.setenv("SWITCHYARD_ENGINE_POLICY", "strict")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: answers("8.8.8.8"))
    sent: list[str] = []

    def send(message: Any, address: str, **kwargs: Any) -> Any:
        sent.append(address)
        return dns.message.make_response(message)

    monkeypatch.setattr(dns.query, "tcp", send)
    client = Rfc2136("dns.example.test", "example.test", "acme-key", "c2VjcmV0")
    await client.create_txt("_acme-challenge.example.test", "challenge")
    assert sent == ["8.8.8.8"]


class EndlessDocument(httpx.AsyncByteStream):
    """A receiver that must be stopped before the entire response is downloaded."""

    def __init__(self) -> None:
        """Track how much of the body was consumed and whether it was closed."""
        self.chunks = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield more than the allowed document size."""
        for _ in range(20):
            self.chunks += 1
            yield b"x" * (MAX_BYTES // 4)

    async def aclose(self) -> None:
        """Record response cleanup."""
        self.closed = True


async def test_oversized_jwks_is_stopped_while_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    body = EndlessDocument()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: answers("8.8.8.8"))

    async def send(self: Any, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=body)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    with pytest.raises(JwksError, match="more than 256 KiB"):
        await http_fetch("https://tenant.example.test/jwks.json")
    assert body.chunks == 5
    assert body.closed


async def test_slow_jwks_has_a_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowDocument(EndlessDocument):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            while True:
                yield b" "
                await asyncio.sleep(0.01)

    body = SlowDocument()
    monkeypatch.setattr("switchyard.services.jwks_sync.FETCH_TIMEOUT_S", 0.5)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: answers("8.8.8.8"))

    async def send(self: Any, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=body)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    with pytest.raises(JwksError, match="fetch exceeded"):
        await http_fetch("https://tenant.example.test/jwks.json")
    assert body.closed


async def test_jwks_refuses_compression_before_decompressing_untrusted_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = EndlessDocument()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: answers("8.8.8.8"))

    async def send(self: Any, request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=body)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    with pytest.raises(JwksError, match="must be uncompressed"):
        await http_fetch("https://tenant.example.test/jwks.json")
    assert body.chunks == 0
    assert body.closed


@pytest.mark.parametrize("url", ["http://8.8.8.8/jwks", "https://user:password@8.8.8.8/jwks"])
async def test_jwks_rejects_plaintext_or_url_credentials(url: str) -> None:
    with pytest.raises(JwksError, match="https without credentials"):
        await http_fetch(url)


async def test_pinning_preserves_real_tls_hostname_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tenant.example.test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("tenant.example.test")]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    # This test isolates TLS verification; separate tests exercise address-policy rejection.
    monkeypatch.setattr(
        "switchyard.adapters.endpoint.resolve_endpoint", lambda *a, **kw: ["127.0.0.1"]
    )
    server = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(
            verify=client_context,
            trust_env=False,
            event_hooks={"request": [partial(pin_request, what="test", env="TEST_CIDRS")]},
        ) as client:
            assert (await client.get(f"https://tenant.example.test:{port}/")).status_code == 200
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await client.get(f"https://wrong.example.test:{port}/")
    finally:
        server.close()
        await server.wait_closed()

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Alert senders: webhook signing, chat formats, address policy at send time (ADR 0121)."""

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import httpx
import pytest

from switchyard.adapters.notify import (
    EVENT_HEADER,
    SIGNATURE_HEADER,
    FakeNotifier,
    HttpNotifier,
    NotifyError,
    SmtpSettings,
    chat_body,
    sign,
)
from switchyard.domain.alerts import Alert

NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
ALERT = Alert(
    "acme", "certs", "firing", "cert:shop", "Certificate shop expires", "Renew it.", NOW, {"d": 3}
)


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def recording(into: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        into.append(request)
        return httpx.Response(200)

    return handler


async def test_a_webhook_gets_a_stable_json_body_and_a_signature() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    notifier = HttpNotifier(client=client(handler), allow_private=True)
    await notifier.send("webhook", {"url": "https://hooks.example.test/in"}, "s3cret", ALERT)
    request = seen[0]
    body = json.loads(request.content)
    assert body == {
        "version": 1,
        "tenant": "acme",
        "rule": "certs",
        "state": "firing",
        "key": "cert:shop",
        "title": "Certificate shop expires",
        "text": "Renew it.",
        "at": NOW.isoformat(),
        "details": {"d": 3},
    }
    expected = hmac.new(b"s3cret", request.content, hashlib.sha256).hexdigest()
    assert request.headers[SIGNATURE_HEADER] == f"sha256={expected}"
    assert request.headers[EVENT_HEADER] == "firing"
    assert request.headers["Connection"] == "close"
    assert sign("s3cret", request.content) == request.headers[SIGNATURE_HEADER]


async def test_no_secret_means_no_signature() -> None:
    seen: list[httpx.Request] = []
    notifier = HttpNotifier(client=client(recording(seen)), allow_private=True)
    await notifier.send("webhook", {"url": "https://hooks.example.test/in"}, None, ALERT)
    assert SIGNATURE_HEADER not in seen[0].headers


async def test_a_webhook_reply_body_is_never_downloaded() -> None:
    class UnboundedReply(httpx.AsyncByteStream):
        chunks = 0
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(20):
                self.chunks += 1
                yield b"x" * (1024 * 1024)

        async def aclose(self) -> None:
            self.closed = True

    reply = UnboundedReply()
    async with client(lambda r: httpx.Response(200, stream=reply)) as http:
        notifier = HttpNotifier(client=http, allow_private=True)
        await notifier.send("webhook", {"url": "https://hooks.example.test/in"}, None, ALERT)
    assert reply.chunks == 0
    assert reply.closed


async def test_slow_webhook_headers_have_a_total_deadline() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as http:
        notifier = HttpNotifier(client=http, allow_private=True, timeout=0.01)
        with pytest.raises(NotifyError, match="TimeoutError"):
            await notifier.send("webhook", {"url": "https://hooks.example.test/in"}, None, ALERT)


@pytest.mark.parametrize(
    ("fmt", "key"), [("slack", "text"), ("mattermost", "text"), ("teams", "title")]
)
def test_chat_formats(fmt: str, key: str) -> None:
    body = chat_body(fmt, ALERT)
    assert "Certificate shop expires" in body[key]
    resolved = chat_body(fmt, Alert("a", "r", "resolved", "k", "Back", "ok", NOW))
    assert "Resolved" in json.dumps(resolved)


async def test_the_receivers_error_is_reported_without_leaking_the_url() -> None:
    notifier = HttpNotifier(client=client(lambda r: httpx.Response(500)), allow_private=True)
    with pytest.raises(NotifyError, match="HTTP 500") as err:
        await notifier.send(
            "chat", {"url": "https://hooks.example.test/T000/B000/SECRET"}, None, ALERT
        )
    assert "SECRET" not in str(err.value)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(NotifyError, match="ConnectError"):
        await HttpNotifier(client=client(boom), allow_private=True).send(
            "webhook", {"url": "https://hooks.example.test/in"}, None, ALERT
        )


async def test_private_destinations_are_refused_at_send_time() -> None:
    called: list[httpx.Request] = []
    notifier = HttpNotifier(client=client(recording(called)))
    for url in ("https://10.0.0.5/x", "https://169.254.169.254/latest", "https://localhost/x"):
        with pytest.raises(NotifyError, match="private"):
            await notifier.send("webhook", {"url": url}, None, ALERT)
    assert called == []


async def test_a_name_that_resolves_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def fake_getaddrinfo(self, host, port, **kw):  # type: ignore[no-untyped-def]
        return [(2, 1, 6, "", ("10.9.9.9", 0))]

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", fake_getaddrinfo)
    notifier = HttpNotifier(client=client(lambda r: httpx.Response(200)))
    with pytest.raises(NotifyError, match="resolves to a private"):
        await notifier.send("webhook", {"url": "https://rebind.example.test/x"}, None, ALERT)


async def test_the_request_goes_to_the_address_that_was_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS rebinding: a name that answers public once and private the next time."""
    import asyncio

    answers = iter(["93.184.216.34", "10.9.9.9", "10.9.9.9"])

    async def rebinding_getaddrinfo(self, host, port, **kw):  # type: ignore[no-untyped-def]
        return [(2, 1, 6, "", (next(answers), 0))]

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", rebinding_getaddrinfo)
    seen: list[httpx.Request] = []
    notifier = HttpNotifier(client=client(recording(seen)))
    await notifier.send("webhook", {"url": "https://rebind.example.test:8443/in?x=1"}, None, ALERT)
    request = seen[0]
    # connected to the checked address, not to the name a second lookup could redirect
    assert request.url.host == "93.184.216.34" and request.url.port == 8443
    assert request.url.path == "/in" and request.url.query == b"x=1"
    assert request.headers["host"] == "rebind.example.test:8443"
    assert request.extensions["sni_hostname"] == "rebind.example.test"


async def test_an_ipv6_answer_is_bracketed_and_redirects_are_not_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def v6(self, host, port, **kw):  # type: ignore[no-untyped-def]
        return [(10, 1, 6, "", ("2606:2800:220:1::1", 0, 0, 0))]

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", v6)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://10.0.0.5/internal"})

    notifier = HttpNotifier(client=client(handler))
    with pytest.raises(NotifyError, match="HTTP 302"):
        await notifier.send("webhook", {"url": "https://hooks.example.test/in"}, None, ALERT)
    assert len(seen) == 1 and seen[0].url.host == "2606:2800:220:1::1"


async def test_email_needs_a_mail_server_and_sends_through_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(NotifyError, match="no mail server"):
        await HttpNotifier().send("email", {"to": ["ops@example.test"]}, None, ALERT)
    sent: list[object] = []

    class FakeSmtp:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            sent.append((host, port))

        def __enter__(self) -> "FakeSmtp":
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def starttls(self) -> None:
            sent.append("starttls")

        def login(self, user: str, password: str) -> None:
            sent.append(("login", user))

        def send_message(self, message: object) -> None:
            sent.append((message["To"], message["Subject"]))  # type: ignore[index]

    monkeypatch.setattr("switchyard.adapters.notify.smtplib.SMTP", FakeSmtp)
    notifier = HttpNotifier(smtp=SmtpSettings("mail.example.test", user="u", password="p"))
    await notifier.send("email", {"to": ["a@example.test", "b@example.test"]}, None, ALERT)
    assert sent == [
        ("mail.example.test", 587),
        "starttls",
        ("login", "u"),
        ("a@example.test, b@example.test", "[Switchyard acme] Certificate shop expires"),
    ]


async def test_the_fake_records_and_fails_on_demand() -> None:
    fake = FakeNotifier(fail={"https://bad.example.test/x"})
    await fake.send("webhook", {"url": "https://ok.example.test/x"}, None, ALERT)
    with pytest.raises(NotifyError):
        await fake.send("webhook", {"url": "https://bad.example.test/x"}, None, ALERT)
    assert len(fake.sent) == 1

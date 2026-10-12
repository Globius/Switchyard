"""Sending an alert to a webhook, a chat service or an email address (ADR 0121).

:class:`HttpNotifier` is the real sender and :class:`FakeNotifier` records what it was asked to
send. Webhook and chat URLs are tenant input, so before each send the host is resolved and refused
when any answer is a private address (unless the operator allowed that), which keeps a tenant from
pointing the controller at the management network or the cloud metadata address.
"""

import asyncio
import hashlib
import hmac
import json
import smtplib
import socket
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from switchyard.domain.alerts import Alert, private_address

SIGNATURE_HEADER = "X-Switchyard-Signature"
EVENT_HEADER = "X-Switchyard-Event"


_MAIL_BODY = "{text}\n\nRule: {rule}\nAbout: {key}\n{at}\n"


class NotifyError(Exception):
    """The alert could not be delivered; the message is safe to show to the tenant."""


@dataclass(frozen=True)
class SmtpSettings:
    """Where email goes (``SWITCHYARD_SMTP_*``); email targets are refused when there is none."""

    host: str
    port: int = 587
    user: str | None = None
    password: str | None = None
    sender: str = "switchyard@localhost"
    starttls: bool = True


class Notifier(Protocol):
    """What the alerting service needs: send one alert through one target."""

    async def send(
        self, kind: str, config: dict[str, Any], secret: str | None, alert: Alert
    ) -> None:
        """Deliver ``alert``; raise :class:`NotifyError` on failure."""
        ...


def chat_body(fmt: str, alert: Alert) -> dict[str, Any]:
    r"""The JSON a chat service's incoming webhook expects.

    Example:
        >>> from datetime import UTC, datetime
        >>> a = Alert("acme", "r", "firing", "k", "Title", "Text", datetime(2026, 1, 1, tzinfo=UTC))
        >>> chat_body("slack", a)["text"]
        '*Title*\nText'
    """
    prefix = {"firing": "", "resolved": "Resolved: ", "test": "Test: "}.get(alert.state, "")
    title = f"{prefix}{alert.title}"
    if fmt == "teams":
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "title": title,
            "text": alert.text,
        }
    if fmt == "mattermost":
        return {"text": f"**{title}**\n{alert.text}"}
    return {"text": f"*{title}*\n{alert.text}"}


def sign(secret: str, body: bytes) -> str:
    """``sha256=<hex>`` HMAC of the body, for the webhook signature header.

    Example:
        >>> sign("k", b"{}")[:7]
        'sha256='
    """
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class HttpNotifier:
    """Real delivery over HTTP(S) and SMTP."""

    def __init__(
        self,
        *,
        smtp: SmtpSettings | None = None,
        allow_private: bool = False,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        """Create the sender.

        Args:
            smtp: Mail server settings; ``None`` refuses email targets.
            allow_private: Let webhook URLs name private addresses (labs).
            client: Injected HTTP client (tests).
            timeout: Seconds per HTTP request.
        """
        self._smtp = smtp
        self._allow_private = allow_private
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def _check_host(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        if self._allow_private:
            return
        if private_address(host):
            raise NotifyError("the URL names a private or local address")
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise NotifyError(f"cannot resolve {host}") from exc
        for info in infos:
            if private_address(str(info[4][0])):
                raise NotifyError(f"{host} resolves to a private or local address")

    async def send(
        self, kind: str, config: dict[str, Any], secret: str | None, alert: Alert
    ) -> None:
        """Deliver the alert; raises :class:`NotifyError` with a short reason on failure."""
        if kind == "email":
            await self._email(config, alert)
            return
        url = str(config["url"])
        await self._check_host(url)
        headers = {"Content-Type": "application/json", EVENT_HEADER: alert.state}
        payload: dict[str, Any] = (
            chat_body(str(config.get("format", "slack")), alert)
            if kind == "chat"
            else alert.as_json()
        )
        body = json.dumps(payload, sort_keys=True).encode()
        if kind == "webhook" and secret:
            headers[SIGNATURE_HEADER] = sign(secret, body)
        try:
            response = await self._client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise NotifyError(f"request failed: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            raise NotifyError(f"the receiver answered HTTP {response.status_code}")

    async def _email(self, config: dict[str, Any], alert: Alert) -> None:
        smtp = self._smtp
        if smtp is None:
            raise NotifyError("this controller has no mail server configured")
        message = EmailMessage()
        message["From"] = smtp.sender
        message["To"] = ", ".join(config["to"])
        message["Subject"] = f"[Switchyard {alert.tenant}] {alert.title}"
        message.set_content(
            _MAIL_BODY.format(
                text=alert.text, rule=alert.rule or "-", key=alert.key, at=alert.at.isoformat()
            )
        )

        def deliver() -> None:
            with smtplib.SMTP(smtp.host, smtp.port, timeout=15) as server:
                if smtp.starttls:
                    server.starttls()
                if smtp.user and smtp.password:
                    server.login(smtp.user, smtp.password)
                server.send_message(message)

        try:
            await asyncio.to_thread(deliver)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotifyError(f"mail delivery failed: {type(exc).__name__}") from exc


@dataclass
class FakeNotifier:
    """Records alerts; targets named in ``fail`` raise :class:`NotifyError`."""

    sent: list[tuple[str, dict[str, Any], str | None, Alert]] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)

    async def send(
        self, kind: str, config: dict[str, Any], secret: str | None, alert: Alert
    ) -> None:
        """Record the alert, or fail for a configured URL/address."""
        marker = str(config.get("url") or ",".join(config.get("to", [])))
        if marker in self.fail:
            raise NotifyError("the receiver answered HTTP 500")
        self.sent.append((kind, config, secret, alert))

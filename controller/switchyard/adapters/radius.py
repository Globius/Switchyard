# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A RADIUS client for password logins (RFC 2865, RFC 2869 Message-Authenticator).

Only what a login needs: Access-Request with a PAP ``User-Password``, the three answers
(Accept, Reject, Challenge) and the attributes a directory maps to groups. MD5 is what the
protocol prescribes; it is not used here for anything else.

The packet functions are pure; :class:`UdpRadiusClient` is the only part that touches the network,
and :class:`FakeRadiusServer` answers from a table for tests (a UDP server on the loopback).

Example:
    >>> req = build_access_request(b"s3cret-shared", 7, "alice", "pw", nas_identifier="sy",
    ...                            authenticator=bytes(16))
    >>> parse_packet(req).code
    1
"""

import asyncio
import hashlib
import hmac
import ipaddress
import os
import socket
import struct
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

ACCESS_REQUEST = 1
ACCESS_ACCEPT = 2
ACCESS_REJECT = 3
ACCESS_CHALLENGE = 11

USER_NAME = 1
USER_PASSWORD = 2
NAS_IDENTIFIER = 32
FILTER_ID = 11
REPLY_MESSAGE = 18
STATE = 24
CLASS = 25
MESSAGE_AUTHENTICATOR = 80

MAX_PACKET = 4096
MAX_PASSWORD = 128


class RadiusError(Exception):
    """The server could not be reached, or its answer is malformed or not authentic."""


@dataclass(frozen=True)
class Packet:
    """A decoded RADIUS packet: code, identifier, authenticator and attributes in order."""

    code: int
    identifier: int
    authenticator: bytes
    attributes: tuple[tuple[int, bytes], ...]
    raw: bytes = field(default=b"", repr=False)

    def all(self, attr: int) -> list[bytes]:
        """Every value of attribute type ``attr``, in order."""
        return [v for t, v in self.attributes if t == attr]

    def first(self, attr: int) -> bytes | None:
        """The first value of ``attr`` or ``None``."""
        found = self.all(attr)
        return found[0] if found else None


@dataclass(frozen=True)
class RadiusResult:
    """The outcome of one exchange.

    Attributes:
        status: ``accept``, ``reject`` or ``challenge``.
        groups: Values of the configured group attribute (Filter-Id or Class), UTF-8 decoded.
        message: The server's Reply-Message, for a challenge prompt (control characters removed).
        state: The ``State`` to send back with the answer to a challenge.
    """

    status: str
    groups: tuple[str, ...] = ()
    message: str = ""
    state: bytes = b""


def _encode_attrs(attrs: list[tuple[int, bytes]]) -> bytes:
    out = b""
    for kind, value in attrs:
        if not 0 < len(value) <= 253:
            raise RadiusError(f"attribute {kind} has an unusable length")
        out += struct.pack("!BB", kind, len(value) + 2) + value
    return out


def encrypt_password(password: bytes, secret: bytes, authenticator: bytes) -> bytes:
    """Hide a PAP password (RFC 2865 section 5.2): XOR chunks with MD5(secret + previous block).

    Example:
        >>> len(encrypt_password(b"pw", b"secret", bytes(16)))
        16
    """
    if not 0 < len(password) <= MAX_PASSWORD:
        raise RadiusError("the password must be 1 to 128 bytes")
    padded = password + b"\x00" * (-len(password) % 16)
    out = b""
    prev = authenticator
    for i in range(0, len(padded), 16):
        key = hashlib.md5(secret + prev).digest()  # noqa: S324 - mandated by RFC 2865  # nosemgrep: python-lang-crypto-weak-algo-atomic
        block = bytes(a ^ b for a, b in zip(padded[i : i + 16], key, strict=True))
        out += block
        prev = block
    return out


def _message_authenticator(packet: bytes, secret: bytes) -> bytes:
    return hmac.new(secret, packet, "md5").digest()


def build_access_request(
    secret: bytes,
    identifier: int,
    username: str,
    password: str,
    *,
    nas_identifier: str,
    state: bytes = b"",
    authenticator: bytes | None = None,
) -> bytes:
    """Build an Access-Request.

    It carries ``User-Name``, the hidden ``User-Password``, NAS-Identifier and a
    Message-Authenticator (so a forged request cannot ride on a captured one).

    Raises:
        RadiusError: If the user name is empty or longer than the protocol allows.
    """
    ra = authenticator if authenticator is not None else os.urandom(16)
    name = username.encode()
    if not 0 < len(name) <= 253:
        raise RadiusError("the user name must be 1 to 253 bytes")
    attrs: list[tuple[int, bytes]] = [
        (USER_NAME, name),
        (USER_PASSWORD, encrypt_password(password.encode(), secret, ra)),
        (NAS_IDENTIFIER, nas_identifier.encode()),
    ]
    if state:
        attrs.append((STATE, state))
    attrs.append((MESSAGE_AUTHENTICATOR, bytes(16)))
    body = _encode_attrs(attrs)
    length = 20 + len(body)
    head = struct.pack("!BBH", ACCESS_REQUEST, identifier, length) + ra
    packet = head + body
    mac = _message_authenticator(packet, secret)
    return packet[:-16] + mac


def parse_packet(data: bytes) -> Packet:
    """Decode ``data``; reject truncated, oversized or inconsistent packets.

    Raises:
        RadiusError: If the length fields disagree or an attribute overruns the packet.
    """
    if not 20 <= len(data) <= MAX_PACKET:
        raise RadiusError("packet size out of range")
    code, ident, length = struct.unpack("!BBH", data[:4])
    if not 20 <= length <= len(data):
        raise RadiusError("packet length mismatch")
    data = data[:length]
    attrs: list[tuple[int, bytes]] = []
    pos = 20
    while pos < length:
        if pos + 2 > length:
            raise RadiusError("truncated attribute")
        kind, alen = data[pos], data[pos + 1]
        if alen < 2 or pos + alen > length:
            raise RadiusError("bad attribute length")
        attrs.append((kind, data[pos + 2 : pos + alen]))
        pos += alen
    return Packet(code, ident, data[4:20], tuple(attrs), data)


def verify_response(
    data: bytes, request: bytes, secret: bytes, *, require_message_authenticator: bool = True
) -> Packet:
    """Parse an answer and check that it answers ``request`` and was made with ``secret``.

    The Response Authenticator is ``MD5(code, id, length, request authenticator, attributes,
    secret)``, which MD5 collisions can be made to fit (BlastRADIUS, CVE-2024-3596). The
    Message-Authenticator (an HMAC) closes that, so it is required by default; it is verified
    whenever present. Servers that never send one need ``require_message_authenticator=False``.

    Raises:
        RadiusError: If either check fails (the caller treats the exchange as failed).
    """
    packet = parse_packet(data)
    if packet.identifier != request[1]:
        raise RadiusError("identifier mismatch")
    raw = packet.raw
    expected = hashlib.md5(  # noqa: S324 - mandated by RFC 2865  # nosemgrep: python-lang-crypto-weak-algo-atomic
        raw[:4] + request[4:20] + raw[20:] + secret
    ).digest()
    if not hmac.compare_digest(expected, packet.authenticator):
        raise RadiusError("the response is not authentic (wrong shared secret?)")
    mac = packet.first(MESSAGE_AUTHENTICATOR)
    if mac is None and require_message_authenticator:
        raise RadiusError("the response has no Message-Authenticator")
    if mac is not None:
        zeroed = _zero_message_authenticator(packet.raw, request[4:20])
        if not hmac.compare_digest(_message_authenticator(zeroed, secret), mac):
            raise RadiusError("bad Message-Authenticator")
    if packet.code not in (ACCESS_ACCEPT, ACCESS_REJECT, ACCESS_CHALLENGE):
        raise RadiusError("unexpected packet code")
    return packet


def _zero_message_authenticator(raw: bytes, request_authenticator: bytes) -> bytes:
    """The packet with the request authenticator in place and the Message-Authenticator zeroed."""
    out = bytearray(raw[:4] + request_authenticator)
    pos = 20
    while pos < len(raw):
        kind, alen = raw[pos], raw[pos + 1]
        chunk = bytearray(raw[pos : pos + alen])
        if kind == MESSAGE_AUTHENTICATOR:
            chunk[2:] = bytes(len(chunk) - 2)
        out += chunk
        pos += alen
    return bytes(out)


def build_response(
    code: int,
    request: bytes,
    secret: bytes,
    attrs: list[tuple[int, bytes]] | None = None,
    *,
    message_authenticator: bool = True,
) -> bytes:
    """Build a signed answer to ``request`` (used by the fake server and by tests).

    Example:
        >>> req = build_access_request(b"s", 1, "a", "p", nas_identifier="n")
        >>> verify_response(build_response(ACCESS_ACCEPT, req, b"s"), req, b"s").code
        2
        >>> bare = build_response(ACCESS_ACCEPT, req, b"s", message_authenticator=False)
        >>> verify_response(bare, req, b"s", require_message_authenticator=False).code
        2
    """
    items = list(attrs or [])
    if message_authenticator:
        items.insert(0, (MESSAGE_AUTHENTICATOR, bytes(16)))
    body = _encode_attrs(items)
    head = struct.pack("!BBH", code, request[1], 20 + len(body))
    if message_authenticator:
        # HMAC over the packet with the request authenticator in place of the response one
        mac = _message_authenticator(head + request[4:20] + body, secret)
        body = body[:2] + mac + body[18:]
    auth = hashlib.md5(head + request[4:20] + body + secret).digest()  # noqa: S324
    return head + auth + body


def interpret(packet: Packet, group_attribute: str = "filter_id") -> RadiusResult:
    """Turn an answer into a :class:`RadiusResult`.

    Example:
        >>> req = build_access_request(b"s", 1, "a", "p", nas_identifier="n")
        >>> ans = build_response(ACCESS_ACCEPT, req, b"s", [(FILTER_ID, b"staff")])
        >>> interpret(verify_response(ans, req, b"s")).groups
        ('staff',)
    """
    kind = FILTER_ID if group_attribute == "filter_id" else CLASS
    groups = tuple(
        dict.fromkeys(v.decode("utf-8", "replace").strip() for v in packet.all(kind) if v.strip())
    )
    message = "".join(v.decode("utf-8", "replace") for v in packet.all(REPLY_MESSAGE))
    message = "".join(ch for ch in message if ch.isprintable())[:200]
    if packet.code == ACCESS_ACCEPT:
        return RadiusResult("accept", groups=groups)
    if packet.code == ACCESS_CHALLENGE:
        return RadiusResult("challenge", message=message, state=packet.first(STATE) or b"")
    return RadiusResult("reject", message=message)


class RadiusClient(Protocol):
    """What the login gate needs from a RADIUS server."""

    async def authenticate(
        self, username: str, password: str, *, state: bytes = b""
    ) -> RadiusResult:
        """Send one Access-Request; return accept, reject or challenge.

        Raises:
            RadiusError: If no authentic answer arrives (callers must treat that as a failure,
                never as success).
        """
        ...


@dataclass(frozen=True)
class RadiusServer:
    """Where and how to ask: one server, its shared secret and the group attribute."""

    host: str
    port: int
    secret: bytes
    nas_identifier: str = "switchyard"
    timeout_s: float = 3.0
    retries: int = 2
    group_attribute: str = "filter_id"
    require_message_authenticator: bool = True
    #: Allow a server that resolves to a loopback, link-local or similar address (tests only).
    #: Names are checked after resolution, so a tenant cannot aim the gate at the broker host
    #: with a DNS name that points there.
    allow_local: bool = False


def _forbidden(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    return (
        ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved
    )


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: object) -> None:
        self.queue.put_nowait(data)


class UdpRadiusClient:
    """:class:`RadiusClient` over UDP with retransmission (new identifier per exchange)."""

    def __init__(self, server: RadiusServer) -> None:
        """Remember ``server``; sockets are opened per exchange."""
        self._server = server

    async def authenticate(
        self, username: str, password: str, *, state: bytes = b""
    ) -> RadiusResult:
        """See :class:`RadiusClient`."""
        srv = self._server
        request = build_access_request(
            srv.secret,
            os.urandom(1)[0],
            username,
            password,
            nas_identifier=srv.nas_identifier,
            state=state,
        )
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(srv.host, srv.port, type=socket.SOCK_DGRAM)
        except OSError as exc:
            raise RadiusError(f"cannot resolve {srv.host}") from exc
        family, _, _, _, addr = infos[0]
        if not srv.allow_local and _forbidden(str(addr[0])):
            raise RadiusError(f"{srv.host} resolves to an address the gate will not contact")
        remote = (str(addr[0]), int(addr[1]))
        transport, proto = await loop.create_datagram_endpoint(
            _Protocol, remote_addr=remote, family=family
        )
        try:
            for _ in range(srv.retries + 1):
                transport.sendto(request)
                deadline = loop.time() + srv.timeout_s
                while True:
                    left = deadline - loop.time()
                    if left <= 0:
                        break
                    try:
                        data = await asyncio.wait_for(proto.queue.get(), left)
                    except TimeoutError:
                        break
                    try:
                        packet = verify_response(
                            data,
                            request,
                            srv.secret,
                            require_message_authenticator=srv.require_message_authenticator,
                        )
                    except RadiusError:
                        continue  # a stray or forged datagram; keep waiting for the real one
                    return interpret(packet, srv.group_attribute)
        finally:
            transport.close()
        raise RadiusError(f"no answer from {srv.host}:{srv.port}")


# --------------------------------------------------------------------------------------- fake

Handler = Callable[[str, str, bytes], RadiusResult]


class FakeRadiusServer:
    """A RADIUS server on 127.0.0.1 for tests: users map to passwords, groups and challenges.

    Example:
        >>> srv = FakeRadiusServer(b"secret", {"alice": ("pw", ("staff",))})
        >>> srv.port
        0
    """

    def __init__(
        self,
        secret: bytes,
        users: Mapping[str, tuple[str, tuple[str, ...]]],
        *,
        challenge: Mapping[str, str] | None = None,
        sign_responses: bool = True,
    ) -> None:
        """Serve ``users`` (name to password and groups); ``challenge`` maps users to their code.

        ``sign_responses`` False makes it behave like a legacy server that never sends a
        Message-Authenticator.
        """
        self.sign_responses = sign_responses
        self.secret = secret
        self.users = dict(users)
        #: user -> one-time code required after a challenge round
        self.challenge = dict(challenge or {})
        self.port = 0
        self.requests: list[Packet] = []
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> int:
        """Bind a free port and serve in a thread; return the port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.1)
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        """Stop serving."""
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        if self._sock:
            self._sock.close()

    def _decrypt(self, packet: Packet) -> str:
        blob = packet.first(USER_PASSWORD) or b""
        out = b""
        prev = packet.authenticator
        for i in range(0, len(blob), 16):
            key = hashlib.md5(self.secret + prev).digest()  # noqa: S324  # nosemgrep: python-lang-crypto-weak-algo-atomic
            out += bytes(a ^ b for a, b in zip(blob[i : i + 16], key, strict=True))
            prev = blob[i : i + 16]
        return out.rstrip(b"\x00").decode("utf-8", "replace")

    def _serve(self) -> None:
        if self._sock is None:
            return
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(MAX_PACKET)
            except (TimeoutError, OSError):
                continue
            try:
                packet = parse_packet(data)
            except RadiusError:
                continue
            self.requests.append(packet)
            name = (packet.first(USER_NAME) or b"").decode("utf-8", "replace")
            password = self._decrypt(packet)
            state = packet.first(STATE) or b""
            user = self.users.get(name)
            attrs: list[tuple[int, bytes]] = []
            if user is None:
                code = ACCESS_REJECT
            elif name in self.challenge and not state:
                if password != user[0]:
                    code = ACCESS_REJECT
                else:
                    code = ACCESS_CHALLENGE
                    attrs = [
                        (REPLY_MESSAGE, b"Enter your one-time code"),
                        (STATE, b"st-" + name.encode()),
                    ]
            elif name in self.challenge:
                ok = state == b"st-" + name.encode() and password == self.challenge[name]
                code = ACCESS_ACCEPT if ok else ACCESS_REJECT
                attrs = [(FILTER_ID, g.encode()) for g in user[1]] if ok else []
            else:
                ok = password == user[0]
                code = ACCESS_ACCEPT if ok else ACCESS_REJECT
                attrs = [(FILTER_ID, g.encode()) for g in user[1]] if ok else []
            reply = build_response(
                code, data, self.secret, attrs, message_authenticator=self.sign_responses
            )
            self._sock.sendto(reply, addr)

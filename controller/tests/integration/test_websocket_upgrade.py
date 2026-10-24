# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Drive a real WebSocket upgrade through the rendered config.

WebSocket is not a feature the renderer switches on: it falls out of the HTTP/1.1 upgrade and the
``timeout tunnel`` line a pool's ``tunnel_timeout_s`` renders. Nothing else proves that, so this test
speaks RFC 6455 to the frontend:

- 192.0.2.20  edge: HAProxy running the rendered config, plain HTTP on 8080 and 8081
- 192.0.2.51  backend: a small RFC 6455 echo server (``python:3.12-slim``)
- 192.0.2.99  client: the Python speaker below; every measured step is printed as ``<label>.<step>``

Both services route to the same backend and differ only in ``tunnel_timeout_s``:

- ``ws-long`` (8080) uses 3600s, so a connection left idle for longer than the frontend's 10s
  ``timeout http-request`` still carries a frame: the tunnel survives the HTTP request timeout.
- ``ws-short`` (8081) uses 2s, so the same idle connection is closed. Because the config's
  ``defaults`` section carries ``timeout tunnel 1h``, this only holds while the backend really has
  the rendered ``timeout tunnel`` line: delete it and HAProxy inherits the 1h default, the tunnel
  stays open and the test fails. That is what makes the test prove the mechanism rather than the
  config text.
"""

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.render.haproxy import render_haproxy
from tests.haproxy_check import HAPROXY_IMAGE
from tests.integration.test_haproxy_behavior import EDGE, EDGE_START
from tests.lab import SUBNET, Lab

#: Python is the smallest image in the lab that can speak frames without adding a dependency.
WS_IMAGE = "python:3.12-slim"
CLIENT = "192.0.2.99"
BACKEND = "192.0.2.51"
LONG_PORT = 8080
SHORT_PORT = 8081

#: How long the client stays quiet before it uses the connection again: longer than the frontend's
#: 10s ``timeout http-request``/``timeout http-keep-alive`` for the long service, and just past the
#: short pool's 2s tunnel timeout for the other.
LONG_IDLE_S = 11.0
SHORT_IDLE_S = 4.0

#: An RFC 6455 echo server: complete the handshake, then answer every text frame with ``echo:``.
WS_SERVER = r"""
import base64, hashlib, socket, struct, threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def read(sock, buf, n):
    while len(buf) < n:
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError
        buf += chunk
    return buf[:n], buf[n:]

def frame(opcode, payload):
    n = len(payload)
    if n < 126:
        return bytes([0x80 | opcode, n]) + payload
    if n < 65536:
        return bytes([0x80 | opcode, 126]) + struct.pack(">H", n) + payload
    return bytes([0x80 | opcode, 127]) + struct.pack(">Q", n) + payload

def handshake(conn):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.decode().partition(":")
        headers[name.strip().lower()] = value.strip()
    accept = base64.b64encode(
        hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest()
    ).decode()
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    return rest

def serve(conn):
    try:
        buf = handshake(conn)
        if buf is None:
            return
        while True:
            head, buf = read(conn, buf, 2)
            opcode = head[0] & 0x0F
            length = head[1] & 0x7F
            if length == 126:
                raw, buf = read(conn, buf, 2)
                length = struct.unpack(">H", raw)[0]
            elif length == 127:
                raw, buf = read(conn, buf, 8)
                length = struct.unpack(">Q", raw)[0]
            mask = b""
            if head[1] & 0x80:
                mask, buf = read(conn, buf, 4)
            payload, buf = read(conn, buf, length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 8:
                return
            if opcode == 9:
                conn.sendall(frame(10, payload))
                continue
            conn.sendall(frame(1, b"echo:" + payload))
    except (EOFError, OSError):
        pass
    finally:
        conn.close()

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", 8080))
server.listen(16)
while True:
    accepted, _ = server.accept()
    threading.Thread(target=serve, args=(accepted,), daemon=True).start()
"""

#: The RFC 6455 client. It retries only until the handshake succeeds (HAProxy and the backend may
#: still be starting); after that every failure is a result, not a retry.
WS_CLIENT = r"""
import base64, hashlib, os, socket, struct, sys, time

HOST = os.environ.get("WS_HOST", "192.0.2.20")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def wait_for_edge(port, deadline):
    while True:
        try:
            return socket.create_connection((HOST, port), timeout=15)
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.25)

class Ws:
    def __init__(self, port):
        self.port = port
        self.buf = b""

    def open(self):
        deadline = time.time() + 30
        while True:
            self.sock = wait_for_edge(self.port, deadline)
            self.buf = b""
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(
                (
                    f"GET /ws HTTP/1.1\r\nHost: {HOST}:{self.port}\r\n"
                    f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            status = self._head()
            if status == "HTTP/1.1 101 Switching Protocols":
                accept = base64.b64encode(
                    hashlib.sha1((key + GUID).encode()).digest()
                ).decode()
                if self.headers.get("sec-websocket-accept") != accept:
                    raise RuntimeError(f"bad accept: {self.headers}")
                return status, self.headers.get("upgrade", "").lower()
            self.sock.close()
            if time.time() > deadline:
                raise RuntimeError(f"handshake failed: {status}")
            time.sleep(0.25)

    def _head(self):
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("connection closed during the handshake")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        self.headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            self.headers[name.strip().lower()] = value.strip()
        return lines[0]

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("closed")
            self.buf += chunk
        got, self.buf = self.buf[:n], self.buf[n:]
        return got

    def _send(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _recv(self):
        while True:
            b0, b1 = self._need(2)
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._need(8))[0]
            mask = self._need(4) if b1 & 0x80 else b""
            payload = self._need(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 8:
                raise EOFError("close frame")
            if opcode == 9:
                self._send(10, payload)
                continue
            return payload.decode(errors="replace")

    def echo(self, text):
        self._send(1, text.encode())
        return self._recv()

def run(port, idle, label):
    ws = Ws(port)
    try:
        status, upgrade = ws.open()
    except (EOFError, OSError, RuntimeError) as exc:
        print(f"{label}.error: handshake:{type(exc).__name__}: {exc}")
        return
    print(f"{label}.handshake: {status}")
    print(f"{label}.upgrade: {upgrade}")
    print(f"{label}.echo: {ws.echo('hello')}")
    time.sleep(idle)
    try:
        print(f"{label}.after: {ws.echo('after-idle')}")
    except TimeoutError:
        print(f"{label}.after: open:timeout")
    except (EOFError, OSError) as exc:
        print(f"{label}.after: closed:{type(exc).__name__}")
    finally:
        ws.sock.close()

run(8080, float(sys.argv[1]), "survives")
run(8081, float(sys.argv[2]), "expires")
"""


def data() -> dict[str, Any]:
    def pool(pid: str, tunnel: int) -> dict[str, Any]:
        return {
            "id": pid,
            "tenant_id": "t",
            "name": pid,
            "tunnel_timeout_s": tunnel,
            "members": [{"address": BACKEND, "port": 8080}],
        }

    def service(vid: str, port: int, pool_id: str) -> dict[str, Any]:
        return {
            "id": vid,
            "tenant_id": "t",
            "se_group_id": "g",
            "name": vid,
            "protocol": "http",
            "vip": EDGE,
            "ports": [{"port": port}],
            "default_pool_id": pool_id,
        }

    return {
        "tenants": [{"id": "t", "name": "acme"}],
        "networks": [
            {"id": "n", "tenant_id": "t", "name": "lab", "cidr": SUBNET},
            {"id": "m", "tenant_id": "t", "name": "mgmt", "cidr": "192.0.2.0/25"},
        ],
        "se_groups": [
            {
                "id": "g",
                "tenant_id": "t",
                "name": "edge",
                "ha_mode": "vrrp",
                "networks": [
                    {"network_id": "n", "role": "vip"},
                    {"network_id": "m", "role": "mgmt"},
                ],
            }
        ],
        "pools": [pool("long-pool", 3600), pool("short-pool", 2)],
        "virtual_services": [
            service("ws-long", LONG_PORT, "long-pool"),
            service("ws-short", SHORT_PORT, "short-pool"),
        ],
    }


@pytest.fixture(scope="module")
def lab(lab_network: Lab) -> Iterator[Lab]:
    with lab_network.scope():
        lab_network._run(
            WS_IMAGE,
            BACKEND,
            ["python3", "-u", "/ws_server.py"],
            {"ws_server.py": WS_SERVER},
        )
        snapshot = snapshot_from_mapping(data())
        rendered = render_haproxy(snapshot, UUID(symbolic_uuid("g")))
        files = {f"etc/switchyard/{p}": c for p, c in rendered.files.items()}
        lab_network._run(HAPROXY_IMAGE, EDGE, ["sh", "-c", EDGE_START], files, user="root")
        yield lab_network


@pytest.fixture(scope="module")
def results(lab: Lab) -> dict[str, str]:
    container = lab._run(
        WS_IMAGE,
        CLIENT,
        ["python3", "-u", "/ws_client.py", str(LONG_IDLE_S), str(SHORT_IDLE_S)],
        {"ws_client.py": WS_CLIENT},
    )
    container.wait(timeout=180)
    raw = container.logs().decode(errors="replace")
    return dict(line.split(": ", 1) for line in raw.splitlines() if ": " in line) | {"_log": raw}


class TestWebSocketUpgrade:
    def test_the_handshake_is_an_rfc6455_101_switch(self, results: dict[str, str]) -> None:
        status = results.get("survives.handshake")
        assert status == "HTTP/1.1 101 Switching Protocols", results["_log"]
        assert results.get("survives.upgrade") == "websocket", results["_log"]

    def test_frames_travel_in_both_directions(self, results: dict[str, str]) -> None:
        assert results.get("survives.echo") == "echo:hello", results["_log"]

    def test_the_tunnel_survives_past_the_http_request_timeout(
        self, results: dict[str, str]
    ) -> None:
        # The client was silent for longer than `timeout http-request 10s`; a frame still crosses.
        assert results.get("survives.after") == "echo:after-idle", results["_log"]

    def test_an_idle_tunnel_is_closed_by_the_pools_tunnel_timeout(
        self, results: dict[str, str]
    ) -> None:
        assert results.get("expires.echo") == "echo:hello", "the short service must upgrade too"
        # Without the rendered `timeout tunnel 2s` the backend inherits `timeout tunnel 1h` from
        # `defaults`, the tunnel is still open after 4s and this reports a frame instead.
        assert (results.get("expires.after") or "").startswith("closed:"), results["_log"]

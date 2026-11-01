# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Where a published container port can be reached from the test process.

On a developer machine the Docker daemon is local, so a port published on ``127.0.0.1`` is reached at
``127.0.0.1``. In GitLab CI the daemon is a sibling ``docker:dind`` service (``DOCKER_HOST=tcp://docker:2375``):
a port published on the daemon's loopback is unreachable from the job container, and the daemon's address is
the host name ``docker``. ``HOST`` is the address to connect to and ``BIND`` the address to publish on.

Example:
    >>> import os
    >>> os.environ.get("DOCKER_HOST") is None or isinstance(HOST, str)
    True
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
from urllib.parse import urlsplit


def _host() -> str:
    """The address published ports answer on: the override, the daemon's host, or loopback."""
    override = os.environ.get("TESTCONTAINERS_HOST_OVERRIDE")
    if override:
        return override
    docker_host = os.environ.get("DOCKER_HOST", "")
    if docker_host.startswith("tcp://"):
        return urlsplit(docker_host).hostname or "127.0.0.1"
    return "127.0.0.1"


#: Connect here to reach a published port.
HOST = _host()
#: Publish on this address: loopback locally, every interface of the (remote) daemon otherwise.
BIND = "127.0.0.1" if HOST in ("127.0.0.1", "localhost") else "0.0.0.0"  # noqa: S104 - dind only


def _pump(source: socket.socket, sink: socket.socket) -> None:
    """Copy bytes one way until either end closes."""
    try:
        with contextlib.suppress(OSError):
            while data := source.recv(65536):
                sink.sendall(data)
    finally:
        for end in (source, sink):
            with contextlib.suppress(OSError):
                end.close()


def loopback_port(port: int) -> int:
    """A port on ``127.0.0.1`` that reaches the published ``port``.

    Code under test that hardcodes ``127.0.0.1`` (the gate talks to the Dex next to it) cannot be pointed at
    the dind host. With a local daemon the published port already is on loopback and is returned unchanged;
    otherwise a thread forwards a free loopback port to ``HOST:port`` for the rest of the process.
    """
    if HOST in ("127.0.0.1", "localhost"):
        return port
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def accept() -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection((HOST, port), timeout=5)
            except OSError:
                client.close()
                continue
            for pair in ((client, upstream), (upstream, client)):
                threading.Thread(target=_pump, args=pair, daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return int(listener.getsockname()[1])

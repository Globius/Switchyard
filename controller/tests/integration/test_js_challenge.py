# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The JavaScript challenge against a real HAProxy: solve it the way the page does (ADR 0171)."""

import hashlib
import io
import re
import tarfile
import time
import urllib.error
import urllib.request

import docker

from tests.golden_support import render_case
from tests.haproxy_check import HAPROXY_IMAGE


def _tar(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"etc/switchyard/{path}")
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _get(url: str, cookie: str | None = None, data: bytes | None = None) -> tuple[int, str]:
    request = urllib.request.Request(  # noqa: S310
        url, data=data, headers={"Cookie": cookie} if cookie else {}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_the_challenge_gates_requests_until_it_is_solved() -> None:
    files = dict(render_case("24-js-challenge").files)
    cfg = files["haproxy/haproxy.cfg"]
    cfg = re.sub(r"bind [0-9.]+:80", "bind :8080", cfg)
    cfg = re.sub(r"^\s+stats socket .*\n", "", cfg, flags=re.MULTILINE)
    cfg = cfg.replace("retries 3", "retries 0").replace("timeout connect 5s", "timeout connect 1s")
    files["haproxy/haproxy.cfg"] = cfg
    salt = re.search(r"\|(\w{16})\"", cfg).group(1)  # type: ignore[union-attr]
    client = docker.from_env()
    container = client.containers.create(
        HAPROXY_IMAGE,
        command=["haproxy", "-f", "/etc/switchyard/haproxy/haproxy.cfg"],
        ports={"8080/tcp": ("127.0.0.1", None)},
        user="root",
    )
    try:
        container.put_archive("/", _tar(files))
        container.start()
        time.sleep(2)
        container.reload()
        port = container.attrs["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
        base = f"http://127.0.0.1:{port}/"
        status, page = _get(base)
        assert status == 200 and "Checking your browser" in page
        found = re.search(r'ts = "(\d+)", ip = "([^"]+)"', page)
        assert found is not None
        ts, ip = found.groups()
        n = 0
        while not hashlib.sha256(f"{ts}-{n}|{ip}|{salt}".encode()).hexdigest().startswith("0000"):
            n += 1
        # Solved: the request reaches the (absent) backend, so HAProxy answers 503, not the page.
        assert _get(base, f"sy_chal={ts}-{n}")[0] == 503
        assert _get(base, f"sy_chal={ts}-{n + 1}")[1].count("Checking your browser") >= 1
        assert "Checking your browser" in _get(base, f"sy_chal={int(ts) - 7200}-{n}")[1]
        assert _get(base, data=b"x")[0] == 403
        assert _get(base + "api/x")[0] == 503  # skipped paths are never challenged
    finally:
        container.remove(force=True)

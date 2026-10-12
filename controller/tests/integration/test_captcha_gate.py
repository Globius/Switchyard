"""The CAPTCHA gate against a real HAProxy, with Cloudflare's published always-pass test keys."""

import http.client
import io
import re
import socket
import tarfile
import time

import docker
import pytest

from tests.golden_support import render_case
from tests.haproxy_check import HAPROXY_IMAGE

TEST_SECRET = "1x0000000000000000000000000000000AA"  # Turnstile test secret: accepts any token


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


def _request(
    port: str, method: str, path: str, *, cookie: str | None = None, body: str | None = None
) -> tuple[int, str, http.client.HTTPResponse]:
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=30)
    headers = {"Host": "app.example.test"}
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    return response.status, response.read().decode(), response


def _internet() -> bool:
    try:
        socket.create_connection(("challenges.cloudflare.com", 443), timeout=3).close()
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _internet(), reason="needs access to challenges.cloudflare.com")
def test_the_gate_lets_a_verified_client_through() -> None:
    files = dict(render_case("25-captcha").files)
    cfg = files["haproxy/haproxy.cfg"]
    cfg = re.sub(r"bind [0-9.]+:80", "bind :8080", cfg)
    cfg = re.sub(r"^\s+stats socket .*\n", "", cfg, flags=re.MULTILINE)
    cfg = cfg.replace("retries 3", "retries 0").replace("timeout connect 5s", "timeout connect 1s")
    files["haproxy/haproxy.cfg"] = cfg
    files["certs/captcha-app.secret"] = TEST_SECRET + "\n"
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
        status, page, _ = _request(port, "GET", "/")
        assert status == 200 and "Verify you are human" in page and "cf-turnstile" in page
        assert _request(port, "POST", "/x")[0] == 403
        assert _request(port, "GET", "/api/x")[0] == 503  # skipped path reaches the backend
        assert _request(port, "POST", "/.sy/captcha", body="next=%2F")[0] == 400
        status, _, response = _request(
            port, "POST", "/.sy/captcha", body="cf-turnstile-response=anything&next=%2Fdash%3Fa%3D1"
        )
        assert status == 302, container.logs().decode()[-800:]
        assert response.getheader("location") == "/dash?a=1"
        cookie = (response.getheader("set-cookie") or "").split(";")[0]
        assert cookie.startswith("sy_cap=")
        assert _request(port, "GET", "/", cookie=cookie)[0] == 503
        ts, sig = cookie.removeprefix("sy_cap=").split(".")
        assert (
            "Verify you are human"
            in _request(port, "GET", "/", cookie=f"sy_cap={ts}.{'0' * 64}")[1]
        )
        assert (
            "Verify you are human"
            in _request(port, "GET", "/", cookie=f"sy_cap={int(ts) - 7200}.{sig}")[1]
        )
        # An off-site redirect target is replaced by the root path.
        status, _, response = _request(
            port, "POST", "/.sy/captcha", body="cf-turnstile-response=x&next=%2F%2Fevil.example"
        )
        assert status == 302 and response.getheader("location") == "/"
    finally:
        container.remove(force=True)

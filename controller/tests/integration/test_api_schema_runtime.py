"""API schema rules against a real HAProxy: what is let through, what is refused and with what status.

The backends do not exist, so a request that passes every rule ends as ``503`` (no server), and the
statuses 400, 404, 405, 411, 413 and 415 are the schema's own answers (ADR 0197).
"""

import http.client
import io
import re
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import docker
import pytest
import yaml
from docker.models.containers import Container

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.render.haproxy import render_haproxy
from tests.golden_support import GOLDEN_DIR
from tests.haproxy_check import HAPROXY_IMAGE

PASS = 503  # the request got past the schema and found no backend
UUID_OK = "123e4567-e89b-12d3-a456-426614174000"


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


def _files(mode: str) -> dict[str, str]:
    data: dict[str, Any] = yaml.safe_load(
        (GOLDEN_DIR / "27-waf-api-schema" / "input.yaml").read_text("utf-8")
    )
    group = UUID(symbolic_uuid(data.pop("render")["se_group"]))
    data["policy_sets"][0]["api"]["mode"] = mode
    files = dict(render_haproxy(snapshot_from_mapping(data), group).files)
    cfg = files["haproxy/haproxy.cfg"]
    cfg = re.sub(r"bind 192\.0\.2\.80:80", "bind :8080", cfg)
    cfg = re.sub(r"bind 192\.0\.2\.81:80", "bind :8081", cfg)
    cfg = re.sub(r"^\s+stats socket .*\n", "", cfg, flags=re.MULTILINE)
    cfg = re.sub(r"^\s+filter spoe .*\n", "", cfg, flags=re.MULTILINE)
    cfg = cfg.replace("retries 3", "retries 0").replace("timeout connect 5s", "timeout connect 1s")
    files["haproxy/haproxy.cfg"] = cfg
    return files


@pytest.fixture(scope="module", params=["block", "detect"])
def proxy(request: pytest.FixtureRequest) -> Iterator[tuple[str, dict[int, str]]]:
    client = docker.from_env()
    container: Container = client.containers.create(
        HAPROXY_IMAGE,
        command=["haproxy", "-f", "/etc/switchyard/haproxy/haproxy.cfg"],
        ports={"8080/tcp": ("127.0.0.1", None), "8081/tcp": ("127.0.0.1", None)},
        user="root",
    )
    try:
        container.put_archive("/", _tar(_files(request.param)))
        container.start()
        time.sleep(2)
        container.reload()
        ports = container.attrs["NetworkSettings"]["Ports"]
        yield (
            request.param,
            {p: ports[f"{p}/tcp"][0]["HostPort"] for p in (8080, 8081)},  # type: ignore[dict-item]
        )
    finally:
        container.remove(force=True)


def call(
    port: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    chunked: bool = False,
) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=15)
    headers = {"Host": "api.example.test"}
    if content_type:
        headers["Content-Type"] = content_type
    if chunked:
        headers["Transfer-Encoding"] = "chunked"
    connection.request(method, path, body=body, headers=headers, encode_chunked=chunked)
    status = connection.getresponse().status
    connection.close()
    return status


#: (method, path, content type, body size, chunked) -> status when blocking
PETS = [
    ("GET", "/v1/pets?limit=10&status=sold&q=hello", None, None, False, PASS),
    ("GET", "/v1/pets", None, None, False, PASS),
    ("GET", "/v1/pets?limit=abc", None, None, False, 400),
    ("GET", "/v1/pets?limit=1&limit=x", None, None, False, 400),  # parameter pollution
    ("GET", "/v1/pets?evil=1", None, None, False, 400),
    ("GET", "/v1/pets?status=gone", None, None, False, 400),
    ("GET", "/v1/pets/12", None, None, False, PASS),
    ("GET", "/v1/pets/abc", None, None, False, 404),
    ("GET", "/v1/pets/12?x=1", None, None, False, 400),
    ("GET", "/v2/pets", None, None, False, 404),
    ("GET", "/v1/other", None, None, False, 404),
    ("PUT", "/v1/pets", None, None, False, 405),
    ("GET", "/v1/pets/../pets", None, None, False, 404),
    ("GET", "/v1/pets/%2e%2e/pets", None, None, False, 404),
    ("GET", "/v1//pets", None, None, False, 404),
    ("POST", "/v1/pets", "application/json", 10, False, PASS),
    ("POST", "/v1/pets", "application/json; charset=utf-8", 10, False, PASS),
    ("POST", "/v1/pets", "text/plain", 10, False, 415),
    ("POST", "/v1/pets", "application/json", 70_000, False, 413),
    ("POST", "/v1/pets", "application/json", 10, True, 411),
    ("DELETE", f"/v1/pets/{UUID_OK}", None, None, False, PASS),
    ("DELETE", "/v1/pets/5", None, None, False, 405),
]
ORDERS = [
    ("GET", "/orders?page=1", PASS),
    ("GET", "/orders?page=1&extra=2", PASS),  # unknown parameters are allowed on this schema
    ("GET", "/orders", 400),
    ("GET", "/orders?page=x", 400),
    ("GET", "/orders?page=1&flag=maybe", 400),
    ("GET", "/orders/mine", PASS),
    ("GET", "/orders/xyz", PASS),
    ("POST", "/orders", 405),
]


@pytest.mark.parametrize(("method", "path", "ctype", "size", "chunked", "blocked"), PETS)
def test_pets_service(
    proxy: tuple[str, dict[int, str]],
    method: str,
    path: str,
    ctype: str | None,
    size: int | None,
    chunked: bool,
    blocked: int,
) -> None:
    mode, ports = proxy
    body = b"x" * size if size is not None else None
    if chunked:
        body = b"x" * 10
    got = call(ports[8080], method, path, body=body, content_type=ctype, chunked=chunked)
    # in detect mode nothing is refused: the violation is only recorded
    assert got == (blocked if mode == "block" else PASS), (method, path)


@pytest.mark.parametrize(("method", "path", "blocked"), ORDERS)
def test_orders_service_always_blocks(
    proxy: tuple[str, dict[int, str]], method: str, path: str, blocked: int
) -> None:
    _, ports = proxy
    assert call(ports[8081], method, path) == blocked, (method, path)


def test_the_golden_input_is_the_one_used() -> None:
    assert Path(GOLDEN_DIR / "27-waf-api-schema" / "input.yaml").is_file()

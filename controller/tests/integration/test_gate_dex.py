# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The login gate in front of real Dex: a RADIUS user signs in through the OIDC code flow (ADR 0195).

Dex runs the rendered ``authproxy`` configuration in a container; the gate (ASGI app, in process)
sits in front of it as in production; RADIUS is the in-process fake server. The browser side is an
``httpx`` client that talks to the gate, so the check covers what only a real Dex can tell us: the
header names Dex trusts, how it reads the group header, and that ``/callback`` is refused without
the gate's say-so.
"""

import base64
import io
import json
import re
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import docker
import httpx
import pytest
import yaml

from switchyard.adapters.radius import FakeRadiusServer
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.gate import app as gate_app
from switchyard.render.broker import render_broker_files

DEX_IMAGE = "ghcr.io/dexidp/dex:v2.41.1-alpine"
BROKER_URL = "https://auth.example.test/dex"
CLIENT = symbolic_uuid("web-radius")
PATH = "/dex/acme/staff-radius"
REDIRECT = "https://wiki.example.test/oauth2/callback"
SECRETS = {
    "kv/tenants/acme/auth/web-radius": "radius-client-secret",
    "kv/tenants/acme/auth/rs": "radius-secret-1",
    "kv/tenants/acme/auth/otp": "radius-secret-2",
    "kv/tenants/acme/auth/gate": "gate-signing-secret-0123456789",
}


def data() -> dict[str, Any]:
    return {
        "tenants": [{"id": "t", "name": "acme"}],
        "policy_sets": [
            {
                "id": "d-radius",
                "tenant_id": "t",
                "name": "staff-radius",
                "kind": "directory",
                "connector": {
                    "type": "radius",
                    "servers": [
                        {
                            "name": "main",
                            "host": "radius.example.test",
                            "secret_ref": "kv/tenants/acme/auth/rs",
                        },
                        {
                            "name": "otp",
                            "host": "radius.example.test",
                            "secret_ref": "kv/tenants/acme/auth/otp",
                        },
                    ],
                    "email_domain": "example.test",
                    "signing_secret_ref": "kv/tenants/acme/auth/gate",
                },
                "flow": {
                    "steps": [
                        {"name": "password", "server": "main"},
                        {
                            "name": "otp",
                            "kind": "code",
                            "server": "otp",
                            "when": {"groups_any": ["admins"]},
                        },
                    ]
                },
            },
            {
                "id": "ps-sso",
                "tenant_id": "t",
                "name": "sso-radius",
                "kind": "access",
                "oidc": {
                    "directory_id": "d-radius",
                    "redirect_hosts": ["wiki.example.test"],
                    "client_id": "web-radius",
                    "client_secret_ref": "kv/tenants/acme/auth/web-radius",
                    "cookie_secret_ref": "kv/tenants/acme/auth/cookie",
                },
            },
        ],
    }


def _tar(path: str, text: str) -> bytes:
    buffer = io.BytesIO()
    raw = text.encode()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(path)
        info.size = len(raw)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


class Stack:
    def __init__(self, directory: Path, radius: list[FakeRadiusServer]) -> None:
        self.directory, self.radius = directory, radius

    def app(self) -> Any:
        """A gate per test: its HTTP client belongs to the test's event loop."""
        return gate_app.Gate(gate_app.ConfigSet(self.directory, recheck_s=0)).app()


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    main = FakeRadiusServer(
        b"radius-secret-1", {"alice": ("pw", ("staff",)), "root": ("pw", ("admins",))}
    )
    otp = FakeRadiusServer(b"radius-secret-2", {"root": ("123456", ())})
    main_port, otp_port = main.start(), otp.start()
    files = render_broker_files(snapshot_from_mapping(data()), SECRETS, BROKER_URL)
    dex = yaml.safe_load(files["dex/acme-staff-radius.yaml"])
    port = int(dex["web"]["http"].rsplit(":", 1)[1])
    dex["web"]["http"] = f"0.0.0.0:{port}"
    client = docker.from_env()
    container = client.containers.create(
        DEX_IMAGE,
        entrypoint=["sh", "-c", "mkdir -p /var/lib/dex && exec dex serve /etc/dex.yaml"],
        user="root",
        ports={f"{port}/tcp": ("127.0.0.1", None)},
    )
    try:
        container.put_archive("/etc", _tar("dex.yaml", yaml.safe_dump(dex)))
        container.start()
        container.reload()
        host_port = int(container.attrs["NetworkSettings"]["Ports"][f"{port}/tcp"][0]["HostPort"])
        deadline = time.time() + 40
        while True:
            try:
                url = f"http://127.0.0.1:{host_port}{PATH}/.well-known/openid-configuration"
                if httpx.get(url).is_success:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                raise AssertionError("Dex did not start:\n" + container.logs().decode())
            time.sleep(0.5)
        config = json.loads(files["gate/acme-staff-radius.json"])
        config["dex_port"] = host_port
        for server, server_port in (("main", main_port), ("otp", otp_port)):
            config["servers"][server].update(host="127.0.0.1", port=server_port, allow_local=True)
        config["servers"]["main"]["port"] = main_port
        config["servers"]["otp"]["port"] = otp_port
        directory: Path = tmp_path_factory.mktemp("gate")
        (directory / "acme-staff-radius.json").write_text(json.dumps(config))
        yield Stack(directory, [main, otp])
    finally:
        container.remove(force=True)
        main.stop()
        otp.stop()


def _local(location: str) -> str:
    """The path and query of a redirect target, whatever host Dex put in it."""
    parts = urlsplit(location)
    return parts.path + (f"?{parts.query}" if parts.query else "")


async def _sign_in(
    stack: Stack, user: str, secrets: list[str]
) -> tuple[httpx.AsyncClient, dict[str, Any]]:
    browser = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app()),
        base_url="https://auth.example.test",
        follow_redirects=False,
    )
    response = await browser.get(
        f"{PATH}/auth",
        params={
            "client_id": CLIENT,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": "openid email profile groups",
            "state": "s1",
        },
    )
    answers = iter(secrets)
    for _ in range(12):
        if response.status_code == 200 and "csrf" in response.text:
            action = re.search(r'action="([^"]+)"', response.text).group(1)  # type: ignore[union-attr]
            csrf = re.search(r'name=csrf value="([^"]+)"', response.text).group(1)  # type: ignore[union-attr]
            form = {"secret": next(answers), "csrf": csrf}
            if 'name="username"' in response.text:
                form["username"] = user
            response = await browser.post(action, data=form)
            continue
        location = response.headers.get("location", "")
        if location.startswith(REDIRECT):
            code = re.search(r"code=([^&]+)", location).group(1)  # type: ignore[union-attr]
            token = (
                await browser.post(
                    f"{PATH}/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": REDIRECT,
                    },
                    auth=(CLIENT, "radius-client-secret"),
                )
            ).json()
            body = token["id_token"].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            return browser, claims
        assert location, (response.status_code, re.sub(r"<[^>]+>|\s+", " ", response.text)[-900:])
        response = await browser.get(_local(location))
    raise AssertionError("the code flow did not finish")


async def test_a_radius_user_signs_in_and_dex_issues_a_token(stack: Stack) -> None:
    _, claims = await _sign_in(stack, "alice", ["pw"])
    assert claims["email"] == "alice@example.test" and claims["aud"] == CLIENT
    assert claims["iss"] == f"{BROKER_URL}/acme/staff-radius"
    assert sorted(claims["groups"]) == ["staff", "sy-factors-1"]


async def test_the_second_factor_is_asked_for_admins_and_counted(stack: Stack) -> None:
    _, claims = await _sign_in(stack, "root", ["pw", "123456"])
    assert sorted(claims["groups"]) == ["admins", "sy-factors-1", "sy-factors-2"]


async def test_dex_trusts_nobody_but_the_gate(stack: Stack) -> None:
    browser = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app()),
        base_url="https://auth.example.test",
        follow_redirects=False,
    )
    start = await browser.get(
        f"{PATH}/auth",
        params={
            "client_id": CLIENT,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": "openid",
            "state": "x",
        },
    )
    location = start.headers["location"]
    while "callback" not in location:
        location = (await browser.get(_local(location))).headers["location"]
    state = re.search(r"state=([^&]+)", location).group(1)  # type: ignore[union-attr]
    forged = await browser.get(
        f"{PATH}/callback?state={state}",
        headers={"X-Remote-User": "root", "X-Remote-Group": "admins"},
    )
    assert forged.status_code == 302 and "/_gate/login" in forged.headers["location"]

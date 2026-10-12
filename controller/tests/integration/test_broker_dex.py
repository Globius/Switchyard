# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The login broker's configs run in real Dex, and a local user can sign in end to end (ADR 0137).

Starts ``dexidp/dex`` with each rendered config (listening address widened to the container so the
test can reach it), checks that every issuer serves its discovery document, and walks the OIDC
code flow for a local user: the same flow oauth2-proxy performs on an engine.
"""

import base64
import io
import json
import tarfile
import time
from collections.abc import Iterator
from typing import Any

import docker
import httpx
import pytest
import yaml

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.render.broker import render_broker_files

DEX_IMAGE = "ghcr.io/dexidp/dex:v2.41.1-alpine"
BROKER_URL = "https://auth.example.test/dex"
# The test loader turns every ``*_id`` value into a UUID, so the client id is that UUID.
LOCAL_CLIENT = symbolic_uuid("web-local")
#: bcrypt of "password" (from Dex's own example configuration).
HASH = "$2a$10$2b2cU8CPhOTaGrs1HRQuAueS7JTT5ZHsHSzYiFPm1leZck7Mc8T4W"
SELF_SIGNED_CA = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBtjCCAVugAwIBAgITBmyf1XSXNmY/Owua2eiedgPySjAKBggqhkjOPQQDAjA5\n"
    "MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6b24g\n"
    "Um9vdCBDQSAzMB4XDTE1MDUyNjAwMDAwMFoXDTQwMDUyNjAwMDAwMFowOTELMAkG\n"
    "A1UEBhMCVVMxDzANBgNVBAoTBkFtYXpvbjEZMBcGA1UEAxMQQW1hem9uIFJvb3Qg\n"
    "Q0EgMzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABCmXp8ZBf8ANm+gBG1bG8lKl\n"
    "ui2yEujSLtf6ycXYqm0fc4E7O5hrOXwzpcVOho6AF2hiRVd9RFgdszflZwjrZt6j\n"
    "QjBAMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgGGMB0GA1UdDgQWBBSr\n"
    "ttvXBp43rDCGB5Fwx5zEGbF4wDAKBggqhkjOPQQDAgNJADBGAiEA4IWSoxe3jfkr\n"
    "BqWTrBqYaGFy+uGh0PsceGCmQ5nFuMQCIQCcAu/xlJyzlvnrxir4tiz+OpAUFteM\n"
    "YyRIHN8wfdVoOw==\n"
    "-----END CERTIFICATE-----\n"
)


def data() -> dict[str, Any]:
    def access(name: str, directory: str, client: str) -> dict[str, Any]:
        return {
            "id": f"ps-{name}",
            "tenant_id": "t",
            "name": name,
            "kind": "access",
            "oidc": {
                "directory_id": directory,
                "redirect_hosts": ["wiki.example.test", "docs.example.test"],
                "client_id": client,
                "client_secret_ref": f"kv/tenants/acme/auth/{client}",
                "cookie_secret_ref": "kv/tenants/acme/auth/cookie",
            },
        }

    return {
        "tenants": [{"id": "t", "name": "acme"}],
        "policy_sets": [
            {
                "id": "d-local",
                "tenant_id": "t",
                "name": "staff-local",
                "kind": "directory",
                "connector": {
                    "type": "local",
                    "users": [
                        {
                            "username": "alice",
                            "email": "alice@example.test",
                            "password_hash_ref": "kv/tenants/acme/auth/alice-hash",
                        }
                    ],
                },
            },
            {
                "id": "d-ldap",
                "tenant_id": "t",
                "name": "corp-ldap",
                "kind": "directory",
                "connector": {
                    "type": "ldap",
                    "host": "ldap.example.test",
                    "root_ca_pem": SELF_SIGNED_CA,
                    "bind_dn": "cn=svc,dc=example,dc=test",
                    "bind_password_ref": "kv/tenants/acme/auth/ldap-bind",
                    "user_base_dn": "ou=people,dc=example,dc=test",
                    "group_base_dn": "ou=groups,dc=example,dc=test",
                },
            },
            {
                "id": "d-saml",
                "tenant_id": "t",
                "name": "partner-saml",
                "kind": "directory",
                "connector": {
                    "type": "saml",
                    "sso_url": "https://idp.example.test/sso",
                    "ca_pem": SELF_SIGNED_CA,
                },
            },
            access("sso-local", "d-local", "web-local"),
            access("sso-ldap", "d-ldap", "web-ldap"),
            access("sso-saml", "d-saml", "web-saml"),
        ],
    }


SECRETS = {
    "kv/tenants/acme/auth/web-local": "local-client-secret",
    "kv/tenants/acme/auth/web-ldap": "ldap-client-secret",
    "kv/tenants/acme/auth/web-saml": "saml-client-secret",
    "kv/tenants/acme/auth/alice-hash": HASH,
    "kv/tenants/acme/auth/ldap-bind": "bind-password",
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


@pytest.fixture(scope="module")
def instances() -> Iterator[dict[str, str]]:
    """Start one Dex per rendered config; map instance name to its host URL."""
    files = render_broker_files(snapshot_from_mapping(data()), SECRETS, BROKER_URL)
    client = docker.from_env()
    started = []
    urls: dict[str, str] = {}
    try:
        for path, text in files.items():
            if not path.endswith(".yaml"):
                continue
            config = yaml.safe_load(text)
            port = int(config["web"]["http"].rsplit(":", 1)[1])
            config["web"]["http"] = f"0.0.0.0:{port}"
            container = client.containers.create(
                DEX_IMAGE,
                entrypoint=["sh", "-c", "mkdir -p /var/lib/dex && exec dex serve /etc/dex.yaml"],
                user="root",
                ports={f"{port}/tcp": ("127.0.0.1", None)},
            )
            started.append(container)
            container.put_archive("/etc", _tar("dex.yaml", yaml.safe_dump(config)))
            container.start()
            container.reload()
            host_port = container.attrs["NetworkSettings"]["Ports"][f"{port}/tcp"][0]["HostPort"]
            name = path.removeprefix("dex/").removesuffix(".yaml")
            urls[name] = f"http://127.0.0.1:{host_port}"
        deadline = time.time() + 40
        for name, base in urls.items():
            issuer_path = "/dex/" + name.replace("-", "/", 1)
            while True:
                try:
                    if httpx.get(
                        f"{base}{issuer_path}/.well-known/openid-configuration"
                    ).is_success:
                        break
                except httpx.HTTPError:
                    pass
                if time.time() > deadline:
                    logs = [c.logs().decode() for c in started]
                    raise AssertionError("Dex did not start:\n" + "\n".join(logs))
                time.sleep(0.5)
        yield urls
    finally:
        for container in started:
            container.remove(force=True)


def _path(name: str) -> str:
    tenant, directory = name.split("-", 1)
    return f"/dex/{tenant}/{directory}"


@pytest.mark.parametrize("name", ["acme-staff-local", "acme-corp-ldap", "acme-partner-saml"])
def test_every_issuer_serves_its_discovery_document(instances: dict[str, str], name: str) -> None:
    doc = httpx.get(f"{instances[name]}{_path(name)}/.well-known/openid-configuration").json()
    assert doc["issuer"] == f"{BROKER_URL}/acme/{name.removeprefix('acme-')}"
    assert "code" in doc["response_types_supported"]


def test_a_local_user_signs_in_through_the_code_flow(instances: dict[str, str]) -> None:
    base, path = instances["acme-staff-local"], _path("acme-staff-local")
    redirect = "https://wiki.example.test/oauth2/callback"
    with httpx.Client(base_url=base, follow_redirects=False) as http:
        start = http.get(
            f"{path}/auth",
            params={
                "client_id": LOCAL_CLIENT,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": "openid email profile groups",
                "state": "s1",
            },
        )
        location = start.headers["location"]
        for _ in range(3):  # one connector: Dex skips the chooser and shows the password form
            page = http.get(location)
            if page.status_code == 200:
                break
            assert "location" in page.headers, (page.status_code, page.text[:800])
            location = page.headers["location"]
        assert page.status_code == 200 and "password" in page.text.lower(), (
            page.status_code,
            page.text[:600],
        )
        done = http.post(location, data={"login": "alice@example.test", "password": "password"})
        target = done.headers["location"]
        for _ in range(4):  # approval is skipped; follow Dex's own redirects up to the client
            if target.startswith(redirect):
                break
            target = http.get(target).headers["location"]
        assert target.startswith(redirect) and "state=s1" in target
        code = target.split("code=", 1)[1].split("&", 1)[0]
        token = http.post(
            f"{path}/token",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
            auth=(LOCAL_CLIENT, "local-client-secret"),
        ).json()
    body = token["id_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert claims["iss"] == f"{BROKER_URL}/acme/staff-local"
    assert claims["email"] == "alice@example.test" and claims["aud"] == LOCAL_CLIENT


def test_a_foreign_redirect_uri_is_refused(instances: dict[str, str]) -> None:
    base, path = instances["acme-staff-local"], _path("acme-staff-local")
    with httpx.Client(base_url=base, follow_redirects=True) as http:
        page = http.get(
            f"{path}/auth",
            params={
                "client_id": LOCAL_CLIENT,
                "redirect_uri": "https://evil.example.test/cb",
                "response_type": "code",
                "scope": "openid",
                "state": "x",
            },
        )
    assert page.status_code in (400, 401, 403) or "Invalid" in page.text, page.text[:300]


def test_a_wrong_password_is_refused(instances: dict[str, str]) -> None:
    base, path = instances["acme-staff-local"], _path("acme-staff-local")
    redirect = "https://wiki.example.test/oauth2/callback"
    with httpx.Client(base_url=base, follow_redirects=True) as http:
        page = http.get(
            f"{path}/auth",
            params={
                "client_id": LOCAL_CLIENT,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": "openid",
                "state": "x",
            },
        )
        failed = http.post(str(page.url), data={"login": "alice@example.test", "password": "wrong"})
    assert "code=" not in str(failed.url)
    assert "Invalid" in failed.text or "incorrect" in failed.text.lower()

"""ACME providers and DNS credentials: the API, secrets handling and issuing end to end (ADR 0079)."""

import json
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from testcontainers.core.container import DockerContainer

from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.models import AcmeChallenge, CertSource
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.acme_providers import AcmeProviders
from switchyard.services.certificates import CertificateService
from switchyard.services.dns_providers import DnsProviders
from tests.integration.test_certificates import run_latest_change_set
from tests.pipeline_env import TENANT, World, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")
V1 = "/api/v1"
BASE = "https://switchyard.example.test"
ROOT = principal(Role.SYSTEM_ADMIN, "root")
T = "acme"


@pytest.fixture(scope="module")
def directory_url() -> Iterator[str]:
    container = (
        DockerContainer("ghcr.io/letsencrypt/pebble:latest")
        .with_env("PEBBLE_VA_ALWAYS_VALID", "1")
        .with_env("PEBBLE_VA_NOSLEEP", "1")
        .with_exposed_ports(14000)
    )
    with container:
        url = f"https://localhost:{container.get_exposed_port(14000)}/dir"
        for _ in range(60):
            try:
                if httpx.get(url, verify=False, timeout=2).status_code == 200:  # noqa: S501
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        yield url


class CloudflareApi:
    """A Cloudflare that records the TXT records it holds."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, str]] = {}
        self.created: list[str] = []
        self.token = "cf-token"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(
                403, json={"success": False, "errors": [{"message": "bad token"}]}
            )
        path = request.url.path.removeprefix("/client/v4")
        if path == "/zones":
            name = request.url.params["name"]
            found = [{"id": "z1", "name": name}] if name == "example.test" else []
            return httpx.Response(200, json={"success": True, "result": found})
        if path == "/zones/z1/dns_records" and request.method == "POST":
            body = json.loads(request.content)
            rid = f"r{len(self.created) + 1}"
            self.records[rid] = {"name": body["name"], "content": body["content"]}
            self.created.append(body["name"])
            return httpx.Response(200, json={"success": True, "result": {"id": rid}})
        if path.startswith("/zones/z1/dns_records/") and request.method == "DELETE":
            self.records.pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(200, json={"success": True, "result": {}})
        return httpx.Response(404, json={"success": False, "errors": [{"message": "no"}]})


@dataclass
class Rig:
    w: World
    secrets: MemorySecrets
    services: ApiServices
    certs: CertificateService
    cloudflare: CloudflareApi
    client: httpx.AsyncClient
    system: dict[str, str]
    admin: dict[str, str]
    operator: dict[str, str]

    async def sql(self, query: str) -> int:
        async with admin_session(self.w.db.admin) as s:
            return int((await s.execute(text(query))).scalar_one())

    async def create_dns(self, name: str = "cf", **extra: Any) -> dict[str, Any]:
        body = {
            "name": name,
            "type": "cloudflare",
            "zones": ["example.test"],
            "credentials": {"api_token": "cf-token"},
        } | extra
        made = await self.client.post(
            f"{V1}/tenants/{T}/dns-providers", json=body, headers=self.admin
        )
        assert made.status_code == 201, made.text
        return made.json()  # type: ignore[no-any-return]

    async def create_acme(self, url: str, name: str = "pebble", **extra: Any) -> dict[str, Any]:
        body = {
            "name": name,
            "preset": "custom",
            "directory_url": url,
            "email": "pki@example.test",
            "terms_agreed": True,
        }
        made = await self.client.post(
            f"{V1}/acme-providers", json=body | extra, headers=self.system
        )
        assert made.status_code == 201, made.text
        return made.json()  # type: ignore[no-any-return]


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        secrets = MemorySecrets()
        services = build_services(w.db, StaticEngineDirectory({}, {}), secrets)
        cloudflare = CloudflareApi()
        transport = httpx.MockTransport(cloudflare.handler)
        services.acme_providers = AcmeProviders(w.db, secrets, verify=False)
        services.dns_providers = DnsProviders(w.db, secrets, transport=transport)
        certs = CertificateService(
            w.db,
            secrets,
            w.service,
            acme_providers=services.acme_providers,
            dns_providers=services.dns_providers,
            dns_wait_s=0,
        )
        system, _ = await services.tokens.create(ROOT, None, "sys", Role.SYSTEM_ADMIN)
        admin, _ = await services.tokens.create(ROOT, TENANT, "admin", Role.TENANT_ADMIN)
        operator, _ = await services.tokens.create(ROOT, TENANT, "op", Role.OPERATOR)
        app = create_app(services, public_url=BASE)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as client:
            yield Rig(
                w,
                secrets,
                services,
                certs,
                cloudflare,
                client,
                {"Authorization": f"Bearer {system}"},
                {"Authorization": f"Bearer {admin}"},
                {"Authorization": f"Bearer {operator}"},
            )


# -- DNS credentials -------------------------------------------------------------------------


async def test_the_catalogue_describes_every_form(rig: Rig) -> None:
    got = await rig.client.get(f"{V1}/dns-provider-types", headers=rig.operator)
    assert got.status_code == 200
    by_type = {t["type"]: t for t in got.json()}
    assert set(by_type) == {"cloudflare", "route53", "azure", "google", "rfc2136", "powerdns"}
    secret_fields = {f["key"] for f in by_type["route53"]["fields"] if f["secret"]}
    assert secret_fields == {"access_key_id", "secret_access_key", "session_token"}


async def test_a_dns_credential_is_stored_in_the_backend_and_never_returned(rig: Rig) -> None:
    made = await rig.create_dns()
    assert made["credentials_set"] is True and made["version"] == 1
    assert "cf-token" not in json.dumps(made)
    stored = json.loads(await rig.secrets.get("kv/tenants/acme/dns/cf"))
    assert stored == {"api_token": "cf-token"}
    url = f"{V1}/tenants/{T}/dns-providers/{made['id']}"
    got = await rig.client.get(url, headers=rig.operator)
    assert got.status_code == 200 and "cf-token" not in got.text
    assert got.headers["etag"] == '"1"'
    assert [
        p["name"]
        for p in (await rig.client.get(f"{url.rsplit('/', 1)[0]}", headers=rig.operator)).json()
    ] == ["cf"]
    leaked = await rig.sql("SELECT count(*) FROM audit_event WHERE diff::text LIKE '%cf-token%'")
    assert leaked == 0


async def test_only_tenant_admins_manage_dns_credentials(rig: Rig) -> None:
    body = {"name": "x", "type": "cloudflare", "credentials": {"api_token": "t"}}
    denied = await rig.client.post(
        f"{V1}/tenants/{T}/dns-providers", json=body, headers=rig.operator
    )
    assert denied.status_code == 403
    assert denied.json()["required_role"] == "tenant-admin"
    made = await rig.create_dns()
    url = f"{V1}/tenants/{T}/dns-providers/{made['id']}"
    for method in ("patch", "delete"):
        r = await rig.client.request(
            method.upper(),
            url,
            json={} if method == "patch" else None,
            headers=rig.operator | {"If-Match": '"1"'},
        )
        assert r.status_code == 403, method
    assert (await rig.client.post(f"{url}/test", headers=rig.operator)).status_code == 403


async def test_validation_errors_name_the_field(rig: Rig) -> None:
    bad = await rig.client.post(
        f"{V1}/tenants/{T}/dns-providers",
        json={
            "name": "Bad Name",
            "type": "azure",
            "zones": ["not a zone"],
            "config": {"cloud": "mars", "typo": "x"},
            "credentials": {},
        },
        headers=rig.admin,
    )
    assert bad.status_code == 422
    paths = {e["path"] for e in bad.json()["errors"]}
    assert paths >= {
        "name",
        "zones[0]",
        "config.cloud",
        "config.typo",
        "config.azure_tenant_id",
        "credentials.client_secret",
    }
    unknown = await rig.client.post(
        f"{V1}/tenants/{T}/dns-providers",
        json={"name": "x", "type": "nope"},
        headers=rig.admin,
    )
    assert {e["path"] for e in unknown.json()["errors"]} == {"type"}
    await rig.create_dns()
    again = await rig.client.post(
        f"{V1}/tenants/{T}/dns-providers",
        json={"name": "cf", "type": "cloudflare", "credentials": {"api_token": "t"}},
        headers=rig.admin,
    )
    assert again.status_code == 409


async def test_patch_merges_secrets_and_checks_the_version(rig: Rig) -> None:
    made = await rig.create_dns(
        name="r53",
        type="route53",
        zones=[],
        credentials={"access_key_id": "AKIA1", "secret_access_key": "s1"},
    )
    url = f"{V1}/tenants/{T}/dns-providers/{made['id']}"
    assert (await rig.client.patch(url, json={}, headers=rig.admin)).status_code == 428
    stale = await rig.client.patch(url, json={}, headers=rig.admin | {"If-Match": '"7"'})
    assert stale.status_code == 412
    changed = await rig.client.patch(
        url,
        json={"credentials": {"secret_access_key": "s2"}, "zones": ["Example.Test."]},
        headers=rig.admin | {"If-Match": '"1"'},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == 2 and changed.json()["zones"] == ["example.test"]
    assert json.loads(await rig.secrets.get("kv/tenants/acme/dns/r53")) == {
        "access_key_id": "AKIA1",
        "secret_access_key": "s2",
    }


async def test_the_test_endpoint_publishes_and_removes_a_record(rig: Rig) -> None:
    made = await rig.create_dns()
    url = f"{V1}/tenants/{T}/dns-providers/{made['id']}/test"
    ok = await rig.client.post(url, json={}, headers=rig.admin)
    assert ok.status_code == 200 and ok.json()["ok"] is True, ok.text
    assert rig.cloudflare.created == ["_switchyard-test.example.test"]
    assert rig.cloudflare.records == {}, "the test record is removed again"
    assert [(z["zone"], z["ok"]) for z in ok.json()["zones"]] == [("example.test", True)]
    rig.cloudflare.token = "rotated-elsewhere"
    broken = await rig.client.post(url, json={}, headers=rig.admin)
    assert broken.json()["ok"] is False and "bad token" in broken.json()["detail"]
    assert broken.json()["zones"][0]["ok"] is False
    no_zone = await rig.create_dns("nozone", zones=[])
    none = await rig.client.post(
        f"{V1}/tenants/{T}/dns-providers/{no_zone['id']}/test", json={}, headers=rig.admin
    )
    assert none.json()["ok"] is False and "zone" in none.json()["detail"]


# -- ACME providers --------------------------------------------------------------------------


async def test_presets_and_the_provider_form(rig: Rig) -> None:
    presets = (await rig.client.get(f"{V1}/acme-provider-presets", headers=rig.operator)).json()
    by_key = {p["key"]: p for p in presets}
    assert by_key["letsencrypt"]["directory_url"].startswith("https://acme-v02")
    assert by_key["zerossl"]["eab_required"] and by_key["keyfactor"]["auth_mode"] == "oauth"
    assert by_key["keyfactor"]["directory_url"] is None


async def test_registering_a_provider_creates_the_account_key_and_never_returns_secrets(
    rig: Rig, directory_url: str
) -> None:
    made = await rig.create_acme(directory_url, default=True)
    assert made["default"] is True and made["account_registered"] is False
    assert "BEGIN PRIVATE KEY" in await rig.secrets.get("kv/system/acme/pebble/account-key")
    listed = await rig.client.get(f"{V1}/acme-providers", headers=rig.operator)
    assert [p["name"] for p in listed.json()] == ["pebble"], "every signed-in user can list them"
    tested = await rig.client.post(f"{V1}/acme-providers/{made['id']}/test", headers=rig.system)
    assert tested.json()["ok"] is True, tested.text
    got = await rig.client.get(f"{V1}/acme-providers/{made['id']}", headers=rig.system)
    assert got.json()["account_registered"] is True and got.headers["etag"] == '"1"'
    denied = await rig.client.post(
        f"{V1}/acme-providers", json={"name": "x", "preset": "letsencrypt"}, headers=rig.admin
    )
    assert denied.status_code == 403 and denied.json()["required_role"] == "system-admin"


async def test_one_default_and_changes_reset_the_registration(rig: Rig, directory_url: str) -> None:
    first = await rig.create_acme(directory_url, "one", default=True)
    second = await rig.create_acme(directory_url, "two", default=True)
    listed = {
        p["name"]: p
        for p in (await rig.client.get(f"{V1}/acme-providers", headers=rig.system)).json()
    }
    assert listed["one"]["default"] is False and listed["two"]["default"] is True
    assert (
        await rig.client.post(f"{V1}/acme-providers/{first['id']}/test", headers=rig.system)
    ).json()["ok"]
    changed = await rig.client.patch(
        f"{V1}/acme-providers/{first['id']}",
        json={"directory_url": directory_url + "?x=1", "email": "other@example.test"},
        headers=rig.system | {"If-Match": '"1"'},
    )
    assert changed.status_code == 200
    assert changed.json()["account_registered"] is False and changed.json()["version"] == 2
    gone = await rig.client.delete(
        f"{V1}/acme-providers/{second['id']}", headers=rig.system | {"If-Match": '"1"'}
    )
    assert gone.status_code == 204


async def test_provider_validation_names_the_field(rig: Rig) -> None:
    bad = await rig.client.post(
        f"{V1}/acme-providers",
        json={
            "name": "Bad Name",
            "preset": "keyfactor",
            "directory_url": "http://insecure.example.test",
            "email": "nope",
            "oauth": {"token_url": "http://idp.example.test/token"},
        },
        headers=rig.system,
    )
    assert bad.status_code == 422
    paths = {e["path"] for e in bad.json()["errors"]}
    assert paths >= {
        "name",
        "terms_agreed",
        "directory_url",
        "email",
        "oauth.client_id",
        "oauth.client_secret",
        "oauth.token_url",
        "oauth.eab_url",
    }
    eab = await rig.client.post(
        f"{V1}/acme-providers",
        json={"name": "zs", "preset": "zerossl", "terms_agreed": True},
        headers=rig.system,
    )
    assert {e["path"] for e in eab.json()["errors"]} == {"eab_kid"}


async def test_oauth_secrets_go_to_the_backend(rig: Rig) -> None:
    made = await rig.client.post(
        f"{V1}/acme-providers",
        json={
            "name": "kf",
            "preset": "keyfactor",
            "directory_url": "https://acme.kf.example.test/directory",
            "oauth": {
                "token_url": "https://idp.example.test/token",
                "client_id": "switchyard",
                "scope": "acme",
                "eab_url": "https://kf.example.test/ACME/KeyManagement",
            },
            "oauth_client_secret": "kf-secret",
            "terms_agreed": True,
        },
        headers=rig.system,
    )
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["auth_mode"] == "oauth" and body["oauth_secret_set"] is True
    assert body["oauth"]["client_id"] == "switchyard" and "kf-secret" not in made.text
    assert await rig.secrets.get("kv/system/acme/kf/oauth-client-secret") == "kf-secret"


# -- issuing end to end ----------------------------------------------------------------------


async def test_a_certificate_is_issued_through_the_chosen_provider_and_credential(
    rig: Rig, directory_url: str
) -> None:
    await rig.create_acme(directory_url, default=True)
    await rig.create_dns()
    change_set = await rig.certs.issue(
        TENANT, "shop", CertSource.ACME, ("shop.example.test", "*.example.test")
    )
    assert change_set
    assert sorted(rig.cloudflare.created) == [
        "_acme-challenge.example.test",
        "_acme-challenge.shop.example.test",
    ]
    assert rig.cloudflare.records == {}, "the challenge records are removed"
    snapshot = await _latest(rig)
    cert = next(c for c in snapshot if c.name == "shop")
    assert cert.source is CertSource.ACME and cert.challenge is AcmeChallenge.DNS01
    assert cert.acme_provider is None and cert.dns_provider is None
    assert "BEGIN CERTIFICATE" in await rig.secrets.get(cert.secret_ref)


async def _latest(rig: Rig) -> Any:
    await run_latest_change_set(rig.w)
    return (await rig.w.snapshot()).certificates


async def test_issuing_names_the_provider_and_the_pinned_credential(
    rig: Rig, directory_url: str
) -> None:
    await rig.create_acme(directory_url, "pebble")
    await rig.create_dns("pinned", zones=[])
    await rig.certs.issue(
        TENANT,
        "pinned-cert",
        CertSource.ACME,
        ("pin.example.test",),
        acme_provider="pebble",
        challenge=AcmeChallenge.DNS01,
        dns_provider="pinned",
    )
    cert = next(c for c in await _latest(rig) if c.name == "pinned-cert")
    assert (cert.acme_provider, cert.dns_provider) == ("pebble", "pinned")


async def test_missing_providers_and_credentials_are_reported_clearly(
    rig: Rig, directory_url: str
) -> None:
    from switchyard.services.certs import CertificateError

    with pytest.raises(CertificateError, match="no default ACME provider"):
        await rig.certs.issue(TENANT, "x", CertSource.ACME, ("x.example.test",))
    await rig.create_acme(directory_url, default=True)
    with pytest.raises(CertificateError, match=r"no DNS credential serves x.example.test"):
        await rig.certs.issue(TENANT, "x", CertSource.ACME, ("x.example.test",))
    await rig.create_dns("other", zones=["other.test"])
    with pytest.raises(CertificateError, match="no DNS credential serves"):
        await rig.certs.issue(TENANT, "x", CertSource.ACME, ("x.example.test",))
    with pytest.raises(CertificateError, match="does not exist or is disabled"):
        await rig.certs.issue(TENANT, "x", CertSource.ACME, ("x.other.test",), dns_provider="ghost")
    with pytest.raises(CertificateError, match="wildcard"):
        await rig.certs.issue(
            TENANT,
            "w",
            CertSource.ACME,
            ("*.example.test",),
            challenge=AcmeChallenge.HTTP01,
        )


async def test_providers_in_use_cannot_be_removed(rig: Rig, directory_url: str) -> None:
    acme = await rig.create_acme(directory_url, "used", default=True)
    dns = await rig.create_dns("used-dns")
    await rig.certs.issue(
        TENANT,
        "uses-both",
        CertSource.ACME,
        ("u.example.test",),
        acme_provider="used",
        dns_provider="used-dns",
    )
    await _latest(rig)
    blocked_acme = await rig.client.delete(
        f"{V1}/acme-providers/{acme['id']}", headers=rig.system | {"If-Match": '"1"'}
    )
    assert blocked_acme.status_code == 409
    blocked_dns = await rig.client.delete(
        f"{V1}/tenants/{T}/dns-providers/{dns['id']}", headers=rig.admin | {"If-Match": '"1"'}
    )
    assert blocked_dns.status_code == 409


async def test_the_environment_of_earlier_versions_becomes_the_default_provider(
    rig: Rig, directory_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("SWITCHYARD_ACME_DIRECTORY", directory_url)
    monkeypatch.setenv("SWITCHYARD_ACME_EMAIL", "legacy@example.test")
    monkeypatch.setenv("SWITCHYARD_ACME_ACCOUNT_KEY_FILE", str(tmp_path / "acme.key"))
    await rig.create_dns()
    await rig.certs.issue(TENANT, "legacy", CertSource.ACME, ("legacy.example.test",))
    listed = (await rig.client.get(f"{V1}/acme-providers", headers=rig.system)).json()
    assert [(p["name"], p["default"], p["email"]) for p in listed] == [
        ("default", True, "legacy@example.test")
    ]
    assert "BEGIN CERTIFICATE" in await rig.secrets.get(
        next(c for c in await _latest(rig) if c.name == "legacy").secret_ref
    )


async def test_a_pasted_account_key_is_stored_and_checked(rig: Rig, directory_url: str) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from switchyard.adapters.acme import new_account_key

    mine = new_account_key()
    made = await rig.create_acme(directory_url, "pasted", account_key=mine)
    assert await rig.secrets.get("kv/system/acme/pasted/account-key") == mine
    assert mine not in str(made)
    weak = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    for bad in (weak, "not a key"):
        refused = await rig.client.post(
            f"{V1}/acme-providers",
            json={
                "name": "bad",
                "preset": "custom",
                "directory_url": directory_url,
                "account_key": bad,
                "terms_agreed": True,
            },
            headers=rig.system,
        )
        assert refused.status_code == 422
        assert {e["path"] for e in refused.json()["errors"]} == {"account_key"}
    replaced = await rig.client.patch(
        f"{V1}/acme-providers/{made['id']}",
        json={"account_key": new_account_key()},
        headers=rig.system | {"If-Match": '"1"'},
    )
    assert replaced.json()["account_registered"] is False
    assert await rig.secrets.get("kv/system/acme/pasted/account-key") != mine


async def test_the_test_reports_each_step_and_a_private_ca_bundle_is_checked(
    rig: Rig, directory_url: str
) -> None:
    made = await rig.create_acme(directory_url, "stages")
    ok = (
        await rig.client.post(f"{V1}/acme-providers/{made['id']}/test", headers=rig.system)
    ).json()
    assert [(s["name"], s["ok"]) for s in ok["stages"]] == [("directory", True), ("account", True)]
    broken = await rig.create_acme("https://localhost:1/dir", "unreachable")
    down = (
        await rig.client.post(f"{V1}/acme-providers/{broken['id']}/test", headers=rig.system)
    ).json()
    assert down["ok"] is False and [s["name"] for s in down["stages"]] == ["directory"]
    bad_bundle = await rig.client.post(
        f"{V1}/acme-providers",
        json={
            "name": "private",
            "preset": "custom",
            "directory_url": directory_url,
            "ca_bundle_pem": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
            "terms_agreed": True,
        },
        headers=rig.system,
    )
    assert {e["path"] for e in bad_bundle.json()["errors"]} == {"ca_bundle_pem"}


async def test_used_by_counts_the_certificates_of_every_tenant(
    rig: Rig, directory_url: str
) -> None:
    made = await rig.create_acme(directory_url, "counted", default=True)
    await rig.create_dns()
    await rig.certs.issue(TENANT, "counted-1", CertSource.ACME, ("c1.example.test",))
    await _latest(rig)
    got = (await rig.client.get(f"{V1}/acme-providers/{made['id']}", headers=rig.system)).json()
    assert got["used_by"] == 0, "a certificate that names no provider does not count"
    await rig.certs.issue(
        TENANT, "counted-2", CertSource.ACME, ("c2.example.test",), acme_provider="counted"
    )
    await _latest(rig)
    got = (await rig.client.get(f"{V1}/acme-providers/{made['id']}", headers=rig.system)).json()
    assert got["used_by"] == 1


async def test_two_credentials_cannot_claim_the_same_zone(rig: Rig) -> None:
    await rig.create_dns("first", zones=["example.test"])
    clash = await rig.client.post(
        f"{V1}/tenants/{T}/dns-providers",
        json={
            "name": "second",
            "type": "cloudflare",
            "zones": ["other.test", "example.test"],
            "credentials": {"api_token": "t"},
        },
        headers=rig.admin,
    )
    assert clash.status_code == 422
    assert clash.json()["errors"][0]["path"] == "zones[1]"
    second = await rig.create_dns("second", zones=["other.test"])
    moved = await rig.client.patch(
        f"{V1}/tenants/{T}/dns-providers/{second['id']}",
        json={"zones": ["example.test"]},
        headers=rig.admin | {"If-Match": '"1"'},
    )
    assert moved.status_code == 422 and moved.json()["errors"][0]["path"] == "zones[0]"
    same = await rig.client.patch(
        f"{V1}/tenants/{T}/dns-providers/{second['id']}",
        json={"zones": ["other.test", "third.test"]},
        headers=rig.admin | {"If-Match": '"1"'},
    )
    assert same.status_code == 200, "a credential may keep its own zones"


async def test_match_lists_the_credentials_that_cover_each_name_longest_zone_first(
    rig: Rig,
) -> None:
    await rig.create_dns("wide", zones=["example.test"])
    await rig.create_dns("narrow", zones=["eu.example.test"])
    await rig.create_dns("catch-all", zones=[])
    await rig.create_dns("off", zones=["off.test"], enabled=False)
    got = await rig.client.get(
        f"{V1}/tenants/{T}/dns-providers/match",
        params=[
            ("names", "a.eu.example.test"),
            ("names", "*.example.test"),
            ("names", "x.off.test"),
        ],
        headers=rig.operator,
    )
    assert got.status_code == 200, got.text
    by_name = {m["name"]: [(c["name"], c["zone"]) for c in m["candidates"]] for m in got.json()}
    assert by_name["a.eu.example.test"] == [
        ("narrow", "eu.example.test"),
        ("wide", "example.test"),
        ("catch-all", None),
    ]
    assert by_name["*.example.test"] == [("wide", "example.test"), ("catch-all", None)]
    assert by_name["x.off.test"] == [("catch-all", None)], "disabled credentials are not offered"


# -- requests, history and progress ----------------------------------------------------------


class Stages:
    """A progress recorder that keeps what it was told."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.failed: list[str] = []
        self.notes: list[str] = []

    def stage(self, name: str) -> Any:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def run() -> Any:
            self.names.append(name)
            try:
                yield
            except BaseException:
                self.failed.append(name)
                raise

        return run()

    async def note(self, message: str, level: str = "info") -> None:
        self.notes.append(message)


async def test_check_lists_every_problem_with_its_field_path(rig: Rig, directory_url: str) -> None:
    body = {"name": "web", "source": "acme", "sans": ["www.example.test", "www.other.test"]}
    url = f"{V1}/tenants/{T}/certificates/issue/check"
    first = (await rig.client.post(url, json=body, headers=rig.operator)).json()
    assert first["ok"] is False
    assert {p["path"] for p in first["problems"]} == {"acme_provider", "sans[0]", "sans[1]"}
    await rig.create_acme(directory_url, default=True)
    await rig.create_dns(zones=["example.test"])
    second = (await rig.client.post(url, json=body, headers=rig.operator)).json()
    assert [(p["path"], p["message"]) for p in second["problems"]] == [
        ("sans[1]", "no DNS credential covers this name")
    ]
    fine = (
        await rig.client.post(url, json=body | {"sans": ["a.example.test"]}, headers=rig.operator)
    ).json()
    assert fine == {"ok": True, "problems": []}
    wild = (
        await rig.client.post(
            url,
            json=body | {"sans": ["*.example.test"], "challenge": "http-01"},
            headers=rig.operator,
        )
    ).json()
    assert {p["path"] for p in wild["problems"]} >= {"sans[0]"}
    assert (
        await rig.client.post(url, json=body, headers={"Authorization": "Bearer nope"})
    ).status_code == 401


async def test_issuing_through_the_api_refuses_what_the_check_refuses(rig: Rig) -> None:
    refused = await rig.client.post(
        f"{V1}/tenants/{T}/certificates/issue",
        json={"name": "web", "source": "acme", "sans": ["www.example.test"]},
        headers=rig.operator,
    )
    assert refused.status_code == 422
    assert {e["path"] for e in refused.json()["errors"]} >= {"acme_provider", "sans[0]"}


async def test_every_attempt_is_recorded_with_the_error_and_the_stages_are_reported(
    rig: Rig, directory_url: str
) -> None:
    from switchyard.services.certs import CertificateError

    await rig.create_acme(directory_url, default=True)
    await rig.create_dns()
    stages = Stages()
    await rig.certs.issue(
        TENANT,
        "history",
        CertSource.ACME,
        ("h.example.test",),
        renew_before_days=45,
        progress=stages,
    )
    assert stages.names == ["order", "validate", "finalize", "store", "change set"]
    assert "challenge records removed" in stages.notes
    await run_latest_change_set(rig.w)
    cert = next(c for c in (await rig.w.snapshot()).certificates if c.name == "history")
    assert cert.renew_before_days == 45 and cert.renew_policy.value == "auto"

    rig.cloudflare.token = "revoked"
    failing = Stages()
    with pytest.raises(CertificateError):
        await rig.certs.issue(
            TENANT,
            "history",
            CertSource.ACME,
            ("h2.example.test",),
            trigger="auto",
            progress=failing,
        )
    assert failing.failed == ["validate"]

    history = (
        await rig.client.get(
            f"{V1}/tenants/{T}/certificates/{cert.id}/renewals", headers=rig.operator
        )
    ).json()
    assert [(r["ok"], r["trigger"]) for r in history] == [(False, "auto"), (True, "manual")]
    assert "bad token" in history[0]["error"] and history[1]["change_set_id"]
    assert history[1]["acme_provider"] is None and history[1]["challenge"] == "dns-01"

    status = (
        await rig.client.get(f"{V1}/tenants/{T}/certificate-renewals", headers=rig.operator)
    ).json()
    mine = next(s for s in status if s["certificate"] == "history")
    assert mine["last_ok"] is False and mine["failures_in_a_row"] == 1
    assert mine["last_success_at"] is not None and "bad token" in mine["last_error"]
    assert mine["renew_before_days"] == 45 and mine["issuer"] == "ACME (default provider)"

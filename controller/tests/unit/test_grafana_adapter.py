# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The Grafana admin client and the pure parts of the embedding service."""

import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from switchyard.adapters.grafana import DatasourceSpec, GrafanaError, HttpGrafana
from switchyard.services.grafana import (
    GrafanaConfig,
    GrafanaService,
    grafana_login,
    valid_landing,
)

BASE = "http://grafana.internal:3000"


class Fake:
    """A tiny Grafana: orgs, users and data sources in dicts, every call recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.orgs: dict[str, int] = {}
        self.users: dict[str, int] = {}
        self.sources: list[dict[str, Any]] = []
        self.race = False
        self.members: dict[int, set[str]] = {}
        self.user_orgs: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: Any = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.calls.append((request.method, path, dict(request.headers), body))
        assert request.headers["authorization"].startswith("Basic ")
        if request.method == "GET" and path.startswith("/api/orgs/name/"):
            name = path.rsplit("/", 1)[1].replace("%2D", "-")
            if name in self.orgs:
                return httpx.Response(200, json={"id": self.orgs[name]})
            return httpx.Response(404, json={})
        if request.method == "POST" and path == "/api/orgs":
            if self.race:
                self.orgs[body["name"]] = 9
                return httpx.Response(409, json={})
            self.orgs[body["name"]] = len(self.orgs) + 2
            return httpx.Response(200, json={"orgId": self.orgs[body["name"]]})
        if path == "/api/users/lookup":
            login = request.url.params["loginOrEmail"]
            if login in self.users:
                return httpx.Response(
                    200, json={"id": self.users[login], "email": f"{login}@switchyard.invalid"}
                )
            return httpx.Response(404, json={})
        if path == "/api/admin/users":
            assert len(body["password"]) >= 32
            self.users[body["login"]] = len(self.users) + 1
            return httpx.Response(200, json={"id": self.users[body["login"]]})
        if request.method == "POST" and path.endswith("/users"):
            member = self.members.setdefault(int(path.split("/")[3]), set())
            known = body["loginOrEmail"] in member
            member.add(body["loginOrEmail"])
            return httpx.Response(409 if known else 200, json={})
        if request.method == "GET" and path.startswith("/api/users/") and path.endswith("/orgs"):
            return httpx.Response(200, json=self.user_orgs)
        if request.method == "GET" and path == "/api/datasources":
            return httpx.Response(200, json=self.sources)
        return httpx.Response(200, json={})


def client(fake: Fake) -> HttpGrafana:
    return HttpGrafana(
        BASE, "admin", "pw", client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )


async def test_org_is_looked_up_then_created() -> None:
    fake = Fake()
    grafana = client(fake)
    first = await grafana.ensure_org("sy-1")
    assert await grafana.ensure_org("sy-1") == first
    assert [c[1] for c in fake.calls].count("/api/orgs") == 1


async def test_org_created_by_another_node_meanwhile() -> None:
    fake = Fake()
    fake.race = True
    assert await client(fake).ensure_org("sy-x") == 9


async def test_user_gets_an_unusable_random_password() -> None:
    fake = Fake()
    grafana = client(fake)
    uid = await grafana.ensure_user("sy-abc", "Alice", "sy-abc@switchyard.invalid")
    assert await grafana.ensure_user("sy-abc", "Alice", "x") == uid


async def test_a_user_is_confined_to_one_organization() -> None:
    fake = Fake()
    grafana = client(fake)
    await grafana.ensure_user("sy-abc", "Alice", "sy-abc@switchyard.invalid")
    fake.user_orgs = [
        {"orgId": 7, "name": "sy-tenant"},
        {"orgId": 8, "name": "sy-abc@switchyard.invalid"},  # the personal org Grafana made
        {"orgId": 9, "name": "some other org"},
    ]
    await grafana.confine_user(7, "sy-abc", "Viewer")
    await grafana.confine_user(7, "sy-abc", "Viewer")  # again: 409 becomes a role patch
    deletes = [c[1] for c in fake.calls if c[0] == "DELETE"]
    assert "/api/orgs/8" in deletes  # personal organization deleted outright
    assert "/api/orgs/9/users/1" in deletes  # membership of others removed
    assert not any(d.startswith("/api/orgs/7") for d in deletes)
    assert any(c[0] == "PATCH" and c[3] == {"role": "Viewer"} for c in fake.calls)


async def test_data_sources_are_created_then_updated_with_a_bearer_header() -> None:
    fake = Fake()
    grafana = client(fake)
    spec = DatasourceSpec(
        "prometheus", "Prometheus", "prometheus", "http://c/ds/prometheus", "tok", True
    )
    await grafana.ensure_datasources(4, [spec])
    method, path, headers, body = fake.calls[-1]
    assert (method, path, headers["x-grafana-org-id"]) == ("POST", "/api/datasources", "4")
    assert body["secureJsonData"] == {"httpHeaderValue1": "Bearer tok"}
    assert body["jsonData"] == {"httpHeaderName1": "Authorization"} and body["access"] == "proxy"
    fake.sources = [{"uid": "prometheus"}]
    await grafana.ensure_datasources(4, [spec])
    assert fake.calls[-1][:2] == ("PUT", "/api/datasources/uid/prometheus")


async def test_dashboards_are_overwritten_and_one_becomes_home() -> None:
    fake = Fake()
    await client(fake).ensure_dashboards(4, [{"uid": "d1", "title": "T", "id": 77}], "d1")
    post, put = fake.calls[-2], fake.calls[-1]
    assert post[1] == "/api/dashboards/db" and post[3]["overwrite"] is True
    assert post[3]["dashboard"]["id"] is None
    assert put[1] == "/api/org/preferences" and put[3] == {"homeDashboardUID": "d1"}


async def test_grafana_errors_become_one_error_type() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    grafana = HttpGrafana(
        BASE, "a", "p", client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )
    with pytest.raises(GrafanaError, match="HTTP 500"):
        await grafana.ensure_org("x")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    grafana = HttpGrafana(
        BASE, "a", "p", client=httpx.AsyncClient(transport=httpx.MockTransport(down))
    )
    with pytest.raises(GrafanaError, match="no route"):
        await grafana.ensure_org("x")


def service(secret: str = "k" * 32) -> GrafanaService:
    config = GrafanaConfig(BASE, "admin", "pw", secret, "http://c/internal/ds", "http://p")
    return GrafanaService(None, config, None)  # type: ignore[arg-type]


def test_a_short_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        service("short")


def test_data_source_tokens_name_the_tenant_and_nothing_else() -> None:
    svc, t = service(), UUID(int=42)
    token = svc.ds_token(t)
    assert svc.tenant_for_ds_token(token) == t
    assert svc.tenant_for_ds_token(token + "0") is None
    assert svc.tenant_for_ds_token(token.replace("sygd_", "sy_")) is None
    assert svc.tenant_for_ds_token("sygd_zz.00") is None
    assert service("j" * 32).tenant_for_ds_token(token) is None  # another key
    other = svc.ds_token(UUID(int=43))
    forged = token.split(".")[0] + "." + other.split(".")[1]
    assert svc.tenant_for_ds_token(forged) is None


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("", True),
        ("/explore", True),
        ("/dashboards", True),
        ("/d/sy-tenant-services", True),
        ("/d/sy-tenant-services/virtual-services?var-service=vs_a&from=now-1h", True),
        ("//evil.example", False),
        ("https://evil.example", False),
        ("/api/admin/users", False),
        ("/d/../x", False),
        ("/d/a\nb", False),
        ("/explore?" + "a" * 300, False),
    ],
)
def test_landing_paths(path: str, ok: bool) -> None:
    assert valid_landing(path) is ok


def test_one_grafana_user_per_person_and_tenant() -> None:
    a, b = UUID(int=1), UUID(int=2)
    assert grafana_login("user:x", a) != grafana_login("user:x", b)
    assert grafana_login("user:x", a) != grafana_login("user:y", a)
    assert grafana_login("user:x", None) != grafana_login("user:x", a)
    assert grafana_login("user:x", a).startswith("sy-")
    assert "user" not in grafana_login("user:x", a)  # no personal data in Grafana

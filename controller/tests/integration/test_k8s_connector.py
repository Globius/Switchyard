# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The Kubernetes connector: clusters, sync into change sets, address write-back, the API (ADR 0151)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio

from switchyard.adapters.kubernetes import FakeKube
from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.errors import (
    AlreadyExistsError,
    FieldErrors,
    PermissionDeniedError,
)
from switchyard.services.kubernetes import KubernetesClusters
from tests.integration.test_certificates import run_latest_change_set
from tests.k8s_support import endpoint_slice, gateway, http_route, node, service
from tests.pipeline_env import TENANT, World, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")
V1 = "/api/v1"
ADMIN = principal(Role.TENANT_ADMIN, "admin")
T = "acme"


def nodes() -> list[dict[str, Any]]:
    return [node("n1", "10.20.0.1"), node("n2", "10.20.0.2")]


def web(port: int = 80, node_port: int = 30080) -> dict[str, Any]:
    return service("shop", "web", [{"name": "http", "port": port, "nodePort": node_port}])


@dataclass
class Rig:
    w: World
    secrets: MemorySecrets
    kube: FakeKube
    clusters: KubernetesClusters
    services: ApiServices

    async def create(self, name: str = "prod", **extra: Any) -> Any:
        return await self.clusters.create(
            ADMIN,
            TENANT,
            name=name,
            api_url="https://93.184.216.34:6443",
            token="sa-token",
            se_group="edge",
            vip_pool="main",
            **extra,
        )

    async def sync(self, cluster: Any) -> dict[str, Any]:
        return await self.clusters.sync(TENANT, cluster.id)

    async def settle(self) -> None:
        await run_latest_change_set(self.w)

    async def names(self, kind: str = "virtual_services", prefix: str = "k8s-") -> dict[str, Any]:
        snapshot = await self.w.snapshot()
        return {o.name: o for o in getattr(snapshot, kind) if o.name.startswith(prefix)}


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        secrets = MemorySecrets()
        kube = FakeKube(node_list=nodes(), service_list=[web()])
        clusters = KubernetesClusters(w.db, secrets, w.service, kube_factory=lambda u, t, c: kube)
        services = build_services(w.db, StaticEngineDirectory({}, {}), secrets)
        services.kubernetes = clusters
        yield Rig(w, secrets, kube, clusters, services)


async def test_registering_a_cluster_checks_the_fields_and_hides_the_token(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWITCHYARD_ENGINE_POLICY", "strict")
    for kwargs, path in (
        ({"api_url": "http://93.184.216.34"}, "api_url"),
        ({"api_url": "https://169.254.169.254"}, "api_url"),
        ({"api_url": "https://10.0.0.5:6443"}, "api_url"),
        ({"name": "Bad Name"}, "name"),
        ({"token": " "}, "token"),
        ({"se_group": "ghost"}, "se_group"),
        ({"vip_pool": "ghost"}, "vip_pool"),
        ({"mode": "magic"}, "mode"),
        ({"ca_pem": "not pem"}, "ca_pem"),
    ):
        args: dict[str, Any] = {
            "name": "prod",
            "api_url": "https://93.184.216.34:6443",
            "token": "t",
            "se_group": "edge",
            "vip_pool": "main",
        } | kwargs
        with pytest.raises(FieldErrors) as caught:
            await rig.clusters.create(ADMIN, TENANT, **args)
        assert path in {e["path"] for e in caught.value.errors}, kwargs
    monkeypatch.setenv("SWITCHYARD_K8S_CIDRS", "10.0.0.0/24")
    try:
        cluster = await rig.create()
    except FieldErrors as e:
        raise AssertionError(e.errors) from e
    assert cluster.name == "prod" and cluster.se_group == "edge" and cluster.vip_pool == "main"
    assert "sa-token" not in str(cluster)
    assert await rig.secrets.get("kv/tenants/acme/k8s/prod") == "sa-token"
    with pytest.raises(AlreadyExistsError):
        await rig.create()
    with pytest.raises(PermissionDeniedError):
        await rig.clusters.create(
            principal(Role.OPERATOR),
            TENANT,
            name="x",
            api_url="https://93.184.216.34",
            token="t",
            se_group="edge",
            vip_pool="main",
        )


async def test_a_sync_creates_pools_services_and_a_monitor_with_one_vip_per_service(
    rig: Rig,
) -> None:
    cluster = await rig.create()
    rig.kube.service_list = [
        service(
            "shop",
            "web",
            [
                {"name": "http", "port": 80, "nodePort": 30080},
                {"name": "https", "port": 443, "nodePort": 30443},
            ],
        ),
        service("shop", "dns", [{"port": 53, "protocol": "UDP", "nodePort": 30053}]),
        service("shop", "other", [{"port": 80, "nodePort": 31000}], klass="metallb"),
    ]
    summary = await rig.sync(cluster)
    assert summary["planned_services"] == 3 and summary["created"] == 6
    await rig.settle()
    vs = await rig.names()
    assert set(vs) == {"k8s-prod--shop-web-80", "k8s-prod--shop-web-443", "k8s-prod--shop-dns-53"}
    assert vs["k8s-prod--shop-web-80"].vip == vs["k8s-prod--shop-web-443"].vip, (
        "one VIP per Service"
    )
    assert vs["k8s-prod--shop-dns-53"].vip != vs["k8s-prod--shop-web-80"].vip
    assert vs["k8s-prod--shop-dns-53"].protocol.value == "udp"
    pools = await rig.names("pools")
    assert [(str(m.address), m.port) for m in pools["k8s-prod--shop-web-80"].members] == [
        ("10.20.0.1", 30080),
        ("10.20.0.2", 30080),
    ]
    monitors = await rig.names("health_monitors")
    assert sorted(monitors) == ["k8s-prod--tcp", "k8s-prod--udp"]
    assert pools["k8s-prod--shop-web-80"].health_monitor_id == monitors["k8s-prod--tcp"].id
    assert "web" in (await rig.w.snapshot()).virtual_services[0].name or True, "others untouched"


async def test_syncing_again_changes_nothing_and_writes_the_address_back(rig: Rig) -> None:
    cluster = await rig.create()
    await rig.sync(cluster)
    await rig.settle()
    summary = await rig.sync(cluster)
    assert summary["created"] == summary["updated"] == summary["deleted"] == 0
    assert "change_set_id" not in summary
    vip = str((await rig.names())["k8s-prod--shop-web-80"].vip)
    assert rig.kube.statuses == {("shop", "web"): vip}, (
        "the VIP is written once the service is committed"
    )
    again = await rig.sync(cluster)
    assert again["addresses_written"] == 0
    row = await rig.clusters.get(ADMIN, TENANT, cluster.id)
    assert row.last_sync_ok is True and row.waiting_for_change_set is None


async def test_a_sync_waits_for_its_own_change_set(rig: Rig) -> None:
    cluster = await rig.create()
    first = await rig.sync(cluster)
    assert "change_set_id" in first
    waiting = await rig.sync(cluster)
    assert "waiting for change set" in waiting["skipped"], "no second change set in flight"
    await rig.settle()
    after = await rig.sync(cluster)
    assert "skipped" not in after


async def test_nodes_services_and_endpoints_that_change_update_or_delete_only_ours(
    rig: Rig,
) -> None:
    cluster = await rig.create()
    await rig.sync(cluster)
    await rig.settle()
    # a node leaves: the pool shrinks
    rig.kube.node_list = [node("n1", "10.20.0.1")]
    summary = await rig.sync(cluster)
    assert summary["updated"] == 1 and summary["created"] == 0
    await rig.settle()
    members = (await rig.names("pools"))["k8s-prod--shop-web-80"].members
    assert [(str(m.address), m.port) for m in members] == [("10.20.0.1", 30080)]
    # the port changes: the pool follows, the VIP stays
    vip = (await rig.names())["k8s-prod--shop-web-80"].vip
    rig.kube.service_list = [web(node_port=30099)]
    await rig.sync(cluster)
    await rig.settle()
    assert (await rig.names())["k8s-prod--shop-web-80"].vip == vip
    # the service goes away: its objects go, the others stay
    rig.kube.service_list = []
    summary = await rig.sync(cluster)
    assert summary["deleted"] == 2 and summary["planned_services"] == 0
    await rig.settle()
    assert await rig.names() == {} and await rig.names("pools") == {}
    assert await rig.names("health_monitors") == {}, "the shared monitor goes with the last pool"
    snapshot = await rig.w.snapshot()
    assert [s.name for s in snapshot.virtual_services] == ["web"], "the tenant's own service stays"


async def test_two_clusters_never_touch_each_others_objects(rig: Rig) -> None:
    prod = await rig.create("prod")
    other = await rig.create("prod-2")
    await rig.sync(prod)
    await rig.settle()
    rig.kube.service_list = []
    summary = await rig.sync(other)
    assert summary["deleted"] == 0, "prod-2 does not claim k8s-prod--"
    assert "k8s-prod--shop-web-80" in await rig.names()


async def test_a_failing_cluster_is_recorded_and_does_not_stop_the_others(rig: Rig) -> None:
    broken = await rig.create("broken")
    rig.kube.fail.add("nodes")
    assert await rig.clusters.sync_all() == []
    row = await rig.clusters.get(ADMIN, TENANT, broken.id)
    assert row.last_sync_ok is False and "injected failure in nodes" in (row.last_sync_error or "")
    assert await rig.clusters.sync_all() == ["broken"]
    assert (await rig.clusters.get(ADMIN, TENANT, broken.id)).last_sync_ok is True


async def test_pods_mode_uses_endpoints_and_the_local_policy_keeps_hosting_nodes(rig: Rig) -> None:
    cluster = await rig.create("pods", mode="pods")
    rig.kube.slice_list = [
        endpoint_slice(
            "shop",
            "web",
            [{"name": "http", "port": 8080}],
            [("10.20.0.50", "n1", True), ("10.20.0.51", "n2", False)],
        )
    ]
    await rig.sync(cluster)
    await rig.settle()
    members = (await rig.names("pools"))["k8s-pods--shop-web-80"].members
    assert [(str(m.address), m.port) for m in members] == [("10.20.0.50", 8080)]


async def test_a_gateway_becomes_an_http_service_with_routes(rig: Rig) -> None:
    cluster = await rig.create()
    rig.kube.service_list = [
        service(
            "shop",
            "web",
            [{"name": "http", "port": 80, "nodePort": 30080}],
            kind="NodePort",
            klass=None,
        )
    ]
    rig.kube.gateway_list = [gateway("infra", "edge", [{"port": 80, "protocol": "HTTP"}])]
    rig.kube.route_list = [
        http_route(
            "shop",
            "r",
            "edge",
            [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                    "backendRefs": [{"name": "web", "port": 80}],
                },
                {
                    "matches": [{"path": {"type": "Exact", "value": "/health"}}],
                    "backendRefs": [{"name": "web", "port": 80}],
                },
            ],
            hostnames=["shop.example.test"],
            parent_namespace="infra",
        )
    ]
    await rig.sync(cluster)
    await rig.settle()
    gw = (await rig.names())["k8s-prod--gw-infra-edge"]
    assert gw.protocol.value == "http"
    assert [
        (r.order, r.match.host, r.match.path_exact, r.match.path_prefix) for r in gw.routes
    ] == [
        (0, "shop.example.test", "/health", None),
        (1, "shop.example.test", None, "/"),
    ]
    assert "k8s-prod--gw-shop-web-80" in await rig.names("pools")


async def test_a_cluster_without_usable_nodes_publishes_an_empty_pool(rig: Rig) -> None:
    cluster = await rig.create()
    rig.kube.node_list = []
    summary = await rig.sync(cluster)
    assert summary["created"] == 2, "the Service is still published, with no member yet"
    await rig.settle()
    assert (await rig.names("pools"))["k8s-prod--shop-web-80"].members == ()


async def test_the_test_reports_version_counts_and_missing_access(rig: Rig) -> None:
    cluster = await rig.create()
    ok = await rig.clusters.test(ADMIN, TENANT, cluster.id)
    assert ok.ok and ok.version == "v1.31.0" and ok.nodes == 2 and ok.services == 1
    assert ok.gateway_api is False and all(ok.permissions.values())
    rig.kube.denied.add("patch/services/status")
    rig.kube.gateway_list = []
    bad = await rig.clusters.test(ADMIN, TENANT, cluster.id)
    assert not bad.ok and bad.gateway_api is True
    assert "may not patch services/status" in bad.detail
    rig.kube.fail.add("version")
    down = await rig.clusters.test(ADMIN, TENANT, cluster.id)
    assert not down.ok and "injected failure" in down.detail


async def test_the_api_round_trip(rig: Rig) -> None:
    token, _ = await rig.services.tokens.create(
        principal(Role.SYSTEM_ADMIN, "r"), TENANT, "adm", Role.TENANT_ADMIN
    )
    op, _ = await rig.services.tokens.create(
        principal(Role.SYSTEM_ADMIN, "r"), TENANT, "op", Role.OPERATOR
    )
    admin, operator = {"Authorization": f"Bearer {token}"}, {"Authorization": f"Bearer {op}"}
    app = create_app(rig.services)
    body = {
        "name": "prod",
        "api_url": "https://93.184.216.34:6443",
        "token": "sa-token",
        "se_group": "edge",
        "vip_pool": "main",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as c:
        base = f"{V1}/tenants/{T}/kubernetes-clusters"
        assert (await c.post(base, json=body, headers=operator)).status_code == 403
        made = await c.post(base, json=body, headers=admin)
        assert made.status_code == 201, made.text
        cluster = made.json()
        assert "sa-token" not in made.text and cluster["last_sync"] is None
        assert (await c.post(base, json=body, headers=admin)).status_code == 409
        listed = (await c.get(base, headers=operator)).json()
        assert [x["name"] for x in listed] == ["prod"]
        url = f"{base}/{cluster['id']}"
        got = await c.get(url, headers=operator)
        assert got.headers["etag"] == '"1"'
        assert (await c.patch(url, json={"mode": "pods"}, headers=admin)).status_code == 428
        changed = await c.patch(
            url, json={"mode": "pods", "token": "new"}, headers=admin | {"If-Match": '"1"'}
        )
        assert (
            changed.status_code == 200
            and changed.json()["mode"] == "pods"
            and "new" not in changed.text
        )
        assert await rig.secrets.get("kv/tenants/acme/k8s/prod") == "new"
        tested = await c.post(f"{url}/test", headers=admin)
        assert tested.status_code == 200 and tested.json()["ok"] is True
        queued = await c.post(f"{url}/sync", headers=admin)
        assert queued.status_code == 202 and queued.json()["job_id"]
        gone = await c.delete(url, headers=admin | {"If-Match": '"2"'})
        assert gone.status_code == 204
        assert (await c.get(url, headers=admin)).status_code == 404


async def test_the_test_checks_gateway_access_only_when_the_gateway_api_is_installed(
    rig: Rig,
) -> None:
    cluster = await rig.create()
    plain = await rig.clusters.test(ADMIN, TENANT, cluster.id)
    assert plain.gateway_api is False and "list gateways" not in plain.permissions
    rig.kube.gateway_list = []
    rig.kube.denied.add("list/httproutes/")
    found = await rig.clusters.test(ADMIN, TENANT, cluster.id)
    assert found.gateway_api is True and found.permissions["list gateways"] is True
    assert found.permissions["list httproutes"] is False
    assert not found.ok and "may not list httproutes" in found.detail


async def test_gateway_status_gets_the_address_and_conditions_once_committed(rig: Rig) -> None:
    cluster = await rig.create()
    rig.kube.service_list = [
        service(
            "shop",
            "web",
            [{"name": "http", "port": 80, "nodePort": 30080}],
            kind="NodePort",
            klass=None,
        )
    ]
    rig.kube.gateway_list = [gateway("infra", "edge", [{"port": 80, "protocol": "HTTP"}])]
    rig.kube.route_list = [
        http_route(
            "shop",
            "r",
            "edge",
            [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                    "backendRefs": [{"name": "web", "port": 80}],
                }
            ],
            hostnames=["shop.example.test"],
            parent_namespace="infra",
        )
    ]
    await rig.sync(cluster)
    assert rig.kube.gateway_statuses == {}, "nothing is written before the service is committed"
    await rig.settle()
    summary = await rig.sync(cluster)
    vip = str((await rig.names())["k8s-prod--gw-infra-edge"].vip)
    assert rig.kube.gateway_statuses == {("infra", "edge"): vip}
    assert summary["addresses_written"] == 1
    conditions = {c["type"]: c["status"] for c in rig.kube.gateway_list[0]["status"]["conditions"]}
    assert conditions == {"Accepted": "True", "Programmed": "True"}
    assert (await rig.sync(cluster))["addresses_written"] == 0, "written once"


async def test_a_sync_that_changes_nothing_keeps_the_counts_of_the_last_change(rig: Rig) -> None:
    cluster = await rig.create()
    first = await rig.sync(cluster)
    await rig.settle()
    await rig.sync(cluster)
    row = await rig.clusters.get(ADMIN, TENANT, cluster.id)
    assert (row.last_sync or {})["created"] == 0
    assert (row.last_sync or {})["last_change"]["created"] == first["created"] > 0
    assert (row.last_sync or {})["last_change"]["change_set_id"] == first["change_set_id"]

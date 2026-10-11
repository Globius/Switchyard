"""The Proxmox REST client against a mocked API."""

from urllib.parse import parse_qs

import httpx
import pytest

from switchyard.adapters.proxmox import HttpProxmox, ProxmoxError, SdnSubnet, VmSpec

UPID = "UPID:pve1:0001:0002:0003:qmclone:101:sy@pve!ctl:"


class Api:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, list[str]]]] = []
        self.task_polls = 0
        self.task_exit = "OK"
        self.existing: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api2/json")
        body = parse_qs(request.content.decode())
        self.requests.append((request.method, path, body))
        assert request.headers["authorization"] == "PVEAPIToken=sy@pve!ctl=secret"
        if request.method == "GET" and path in self.existing:
            return httpx.Response(200, json={"data": {}})
        if request.method == "GET" and path == "/cluster/sdn/vnets":
            return httpx.Response(
                200, json={"data": [{"vnet": "v1", "zone": "z1"}, {"vnet": "v2", "zone": "z2"}]}
            )
        if request.method == "GET" and path.endswith("/subnets"):
            return httpx.Response(200, json={"data": []})
        if request.method == "GET" and path == "/nodes":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"node": "pve2", "status": "offline"},
                        {
                            "node": "pve1",
                            "status": "online",
                            "cpu": 0.5,
                            "maxcpu": 8,
                            "mem": 10,
                            "maxmem": 20,
                            "uptime": 99,
                        },
                    ]
                },
            )
        if request.method == "GET" and path == "/nodes/pve1/storage":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "storage": "local-lvm",
                            "type": "lvmthin",
                            "active": 1,
                            "content": "rootdir,images",
                            "total": 100,
                            "used": 40,
                            "avail": 60,
                        },
                        {"storage": "iso", "type": "dir", "active": 0},
                    ]
                },
            )
        if request.method == "GET" and path == "/cluster/nextid":
            return httpx.Response(200, json={"data": "101"})
        if request.method == "GET" and "/tasks/" in path:
            self.task_polls += 1
            if self.task_polls < 2:
                return httpx.Response(200, json={"data": {"status": "running"}})
            done = {"status": "stopped", "exitstatus": self.task_exit}
            return httpx.Response(200, json={"data": done})
        if request.method == "GET":
            return httpx.Response(404, json={"data": None})
        if request.method == "POST" and path.endswith("/clone"):
            return httpx.Response(200, json={"data": UPID})
        return httpx.Response(200, json={"data": None})


def client(api: Api) -> HttpProxmox:
    async def no_sleep(_: float) -> None:
        return None

    return HttpProxmox(
        "https://pve1:8006",
        "sy@pve!ctl",
        "secret",
        transport=httpx.MockTransport(api.handler),
        sleep=no_sleep,
    )


async def test_create_vm_clones_waits_for_the_task_and_configures() -> None:
    api = Api()
    pve = client(api)
    spec = VmSpec(
        name="se-a",
        node="pve1",
        template_vmid=9000,
        mgmt_ip_cidr="10.30.0.11/24",
        gateway="10.30.0.1",
        pool="switchyard",
        nics=(("vnet1", None), ("vnet2", 120)),
    )
    info = await pve.create_vm(spec)
    assert (info.vmid, info.status) == (101, "stopped")
    assert api.task_polls == 2
    clone = next(b for m, p, b in api.requests if p.endswith("/9000/clone"))
    assert clone["newid"] == ["101"] and clone["pool"] == ["switchyard"]
    config = next(b for m, p, b in api.requests if m == "PUT" and p.endswith("/101/config"))
    assert config["ipconfig0"] == ["ip=10.30.0.11/24,gw=10.30.0.1"]
    assert config["net1"] == ["virtio,bridge=vnet1"]
    assert config["net2"] == ["virtio,bridge=vnet2,tag=120"]
    await pve.close()


async def test_failed_task_raises() -> None:
    api = Api()
    api.task_exit = "clone failed"
    with pytest.raises(ProxmoxError, match="clone failed"):
        await client(api).create_vm(VmSpec("x", "pve1", 9000, "10.0.0.2/24"))


async def test_ensure_calls_are_idempotent() -> None:
    api = Api()
    api.existing = {"/cluster/sdn/zones/z1", "/cluster/sdn/vnets/v1"}
    pve = client(api)
    await pve.ensure_zone("z1")
    await pve.ensure_vnet("v1", "z1")
    assert not [r for r in api.requests if r[0] == "POST"]
    await pve.ensure_zone("z2", peers="10.0.0.1,10.0.0.2")
    await pve.ensure_vnet("v2", "z2", tag=100)
    await pve.ensure_subnet("v2", SdnSubnet("192.0.2.0/24", "192.0.2.1"))
    posts = {p: b for m, p, b in api.requests if m == "POST"}
    assert posts["/cluster/sdn/zones"]["peers"] == ["10.0.0.1,10.0.0.2"]
    assert posts["/cluster/sdn/vnets"]["tag"] == ["100"]
    assert posts["/cluster/sdn/vnets/v2/subnets"]["gateway"] == ["192.0.2.1"]


async def test_zone_cleanup_calls() -> None:
    api = Api()
    pve = client(api)
    assert await pve.zone_vnets("z1") == ["v1"]
    await pve.delete_zone("z1")
    assert ("DELETE", "/cluster/sdn/zones/z1") in [(m, p) for m, p, _ in api.requests]
    assert await pve.ensure_zone("z3") is True
    api.existing.add("/cluster/sdn/zones/z4")
    assert await pve.ensure_zone("z4") is False


async def test_destroy_of_a_missing_vm_is_a_no_op_and_errors_surface() -> None:
    api = Api()
    pve = client(api)
    await pve.destroy_vm("pve1", 555)
    assert not [r for r in api.requests if r[0] == "DELETE"]
    assert await pve.get_vm("pve1", 555) is None

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="no")

    broken = HttpProxmox("https://pve1:8006", "a@b!c", "s", transport=httpx.MockTransport(boom))
    with pytest.raises(ProxmoxError, match="HTTP 500"):
        await broken.apply_sdn()


async def test_nodes_and_storage_are_read_and_sorted() -> None:
    pve = client(Api())
    nodes = await pve.list_nodes()
    assert [(n.name, n.online) for n in nodes] == [("pve1", True), ("pve2", False)]
    assert (nodes[0].cpu, nodes[0].max_cpu, nodes[0].mem_total, nodes[0].uptime_s) == (
        0.5,
        8,
        20,
        99,
    )
    assert (nodes[1].cpu, nodes[1].mem_total) == (None, None)
    storage = await pve.list_storage("pve1")
    assert [s.name for s in storage] == ["iso", "local-lvm"]
    lvm = storage[1]
    assert (lvm.kind, lvm.active, lvm.content, lvm.available) == (
        "lvmthin",
        True,
        ("images", "rootdir"),
        60,
    )
    assert (storage[0].active, storage[0].content, storage[0].total) == (False, (), None)

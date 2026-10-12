# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SDN apply and SE group deployment against the fake Proxmox."""

import copy
import uuid
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from switchyard.adapters.proxmox import FakeProxmox
from switchyard.db.tenancy import admin_session
from switchyard.domain.loader import symbolic_uuid
from switchyard.jobs.handlers import build_handlers
from switchyard.jobs.worker import Worker
from switchyard.services.engines import EngineService
from switchyard.services.infra import (
    APPLY_SDN_JOB,
    DeployError,
    DeployRequest,
    InfraJobs,
    InfraService,
)
from tests.factories import base_data
from tests.pipeline_env import GROUP, TENANT, World, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")


def infra_data() -> dict[str, Any]:
    d = copy.deepcopy(base_data())
    d["networks"][0].update(managed=True, sdn_zone="zone1", vnet="vlb", vlan_or_vni=100)
    d["networks"][1].update(managed=True, sdn_zone="zone1", vnet="vbe", vlan_or_vni=200)
    d["networks"].append(
        {
            "id": "net-mgmt",
            "tenant_id": "t-acme",
            "name": "mgmt",
            "cidr": "10.30.0.0/24",
            "gateway": "10.30.0.1",
            "managed": True,
            "sdn_zone": "zone1",
            "vnet": "vmgmt",
            "vlan_or_vni": 300,
        }
    )
    d["vip_pools"].append(
        {
            "id": "vp-mgmt",
            "tenant_id": "t-acme",
            "name": "mgmt-addrs",
            "network_id": "net-mgmt",
            "ranges": [{"start": "10.30.0.10", "end": "10.30.0.20"}],
        }
    )
    d["se_groups"][0]["networks"].append({"network_id": "net-mgmt", "role": "mgmt"})
    return d


def with_nic_pools(d: dict[str, Any]) -> dict[str, Any]:
    """Add an engine address pool on the group's backend network (the VIP pool is for VIPs)."""
    d["vip_pools"].append(
        {
            "id": "vp-backend-se",
            "tenant_id": "t-acme",
            "name": "backend-se",
            "network_id": d["networks"][1]["id"],
            "ranges": [{"start": "10.20.0.200", "end": "10.20.0.210"}],
        }
    )
    return d


def request(count: int = 2, **kw: Any) -> DeployRequest:
    args: dict[str, Any] = {
        "tenant_id": TENANT,
        "se_group_id": GROUP,
        "count": count,
        "nodes": ("pve1", "pve2"),
        "template_vmid": 9000,
        "mgmt_pool_id": uuid.UUID(symbolic_uuid("vp-mgmt")),
        "credentials_ref": "kv/se/dpapi",
    }
    return DeployRequest(**{**args, **kw})


async def count(w: World, sql: str) -> int:
    async with admin_session(w.db.admin) as s:
        return int((await s.execute(text(sql))).scalar_one())


async def test_apply_sdn_creates_objects_once_and_applies(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        infra = InfraService(w.db, pve)
        assert (await infra.apply_sdn(TENANT))["networks"] == ["backend", "lb", "mgmt"]
        assert set(pve.vnets) == {"vlb", "vbe", "vmgmt"} and pve.vnets["vlb"] == ("zone1", 100)
        assert pve.sdn_applied == 1
        await infra.apply_sdn(TENANT)  # idempotent
        assert len(pve.subnets["vlb"]) == 1


async def unmanage(w: World, *names: str) -> None:
    async with admin_session(w.db.admin) as s:
        for name in names:
            await s.execute(text(f"UPDATE network SET managed = false WHERE name = '{name}'"))


async def test_apply_sdn_removes_only_what_the_controller_created(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        infra = InfraService(w.db, pve)
        await infra.apply_sdn(TENANT)
        assert await count(w, "SELECT count(*) FROM sdn_object") == 4  # zone1 + 3 vnets
        await unmanage(w, "mgmt")
        result = await infra.apply_sdn(TENANT)
        assert result["removed"] == ["vnet:vmgmt"]
        assert set(pve.vnets) == {"vlb", "vbe"} and "zone1" in pve.zones
        await unmanage(w, "lb", "backend")
        result = await infra.apply_sdn(TENANT)
        assert result["removed"] == ["vnet:vbe", "vnet:vlb", "zone:zone1"]
        assert not pve.vnets and not pve.zones
        assert await count(w, "SELECT count(*) FROM sdn_object") == 0
        assert (await infra.apply_sdn(TENANT))["removed"] == []  # nothing left to do


async def test_apply_sdn_never_deletes_objects_it_did_not_create(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        pve.zones["zone1"] = "vxlan"  # exists before the controller touches it
        infra = InfraService(w.db, pve)
        await infra.apply_sdn(TENANT)
        assert await count(w, "SELECT count(*) FROM sdn_object WHERE kind = 'zone'") == 0
        await unmanage(w, "lb", "backend", "mgmt")
        await infra.apply_sdn(TENANT)
        assert not pve.vnets and "zone1" in pve.zones


async def test_apply_sdn_keeps_a_zone_that_holds_a_foreign_vnet(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        infra = InfraService(w.db, pve)
        await infra.apply_sdn(TENANT)
        await pve.ensure_vnet("hand-made", "zone1")
        await unmanage(w, "lb", "backend", "mgmt")
        await infra.apply_sdn(TENANT)
        assert set(pve.vnets) == {"hand-made"} and "zone1" in pve.zones


async def test_deploy_creates_vms_registers_engines_and_spreads_over_nodes(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        result = await InfraService(w.db, pve).deploy_se_group(request(2))
        assert result["created"] == ["edge-1", "edge-2"]
        specs = [s for s, _ in pve.vms.values()]
        assert [s.node for s in specs] == ["pve1", "pve2"]
        assert [s.mgmt_ip_cidr for s in specs] == ["10.30.0.10/24", "10.30.0.11/24"]
        assert specs[0].mgmt_bridge == "vmgmt" and specs[0].nics == (("vlb", None), ("vbe", None))
        assert all(status == "running" for _, status in pve.vms.values())
        assert await count(w, "SELECT count(*) FROM service_engine WHERE vmid IS NOT NULL") == 2
        assert await count(w, "SELECT count(*) FROM service_engine WHERE guest_kind = 'qemu'") == 2
        assert (
            await count(
                w, "SELECT count(*) FROM service_engine WHERE agent_url = '10.30.0.10:7443'"
            )
            == 1
        )


async def test_deploy_can_ask_for_containers(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        await InfraService(w.db, pve).deploy_se_group(request(1, guest_kind="lxc"))
        assert [s.kind for s, _ in pve.vms.values()] == ["lxc"]
        assert await count(w, "SELECT count(*) FROM service_engine WHERE guest_kind = 'lxc'") == 1
        roundtrip = DeployRequest.from_payload(request(1, guest_kind="lxc").as_payload())
        assert roundtrip.guest_kind == "lxc"
        assert (
            DeployRequest.from_payload(
                {k: v for k, v in request(1).as_payload().items() if k != "guest_kind"}
            ).guest_kind
            == "qemu"
        )  # jobs queued before this field existed


async def test_deploy_only_adds_the_missing_engines(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        infra = InfraService(w.db, pve)
        await infra.deploy_se_group(request(1))
        again = await infra.deploy_se_group(request(3))
        assert again["existing"] == ["edge-1"] and again["created"] == ["edge-2", "edge-3"]
        assert len(pve.vms) == 3
        assert (await infra.deploy_se_group(request(3)))["created"] == []


async def test_failed_deploy_cleans_up_what_it_created_but_keeps_earlier_engines(
    pg_url: str,
) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        infra = InfraService(w.db, pve)
        await infra.deploy_se_group(request(1))
        pve.fail_on.add("start_vm")
        with pytest.raises(DeployError, match="cleaned up"):
            await infra.deploy_se_group(request(3))
        assert [s.name for s, _ in pve.vms.values()] == ["edge-1"]
        assert await count(w, "SELECT count(*) FROM service_engine WHERE vmid IS NOT NULL") == 1


async def test_deploy_validates_the_request(pg_url: str) -> None:
    wrong_pool = uuid.UUID(symbolic_uuid("vp-main"))
    async with world(pg_url, infra_data()) as w:
        infra = InfraService(w.db, FakeProxmox())
        with pytest.raises(DeployError, match="mgmt_pool_id"):
            await infra.deploy_se_group(request(1, mgmt_pool_id=wrong_pool))
        with pytest.raises(DeployError, match="at least"):
            await infra.deploy_se_group(request(1, nodes=()))


async def test_deploy_needs_a_mgmt_network(pg_url: str) -> None:
    async with world(pg_url) as w:  # base data has no mgmt network
        with pytest.raises(DeployError, match="exactly one mgmt"):
            await InfraService(w.db, FakeProxmox()).deploy_se_group(request(1))


async def test_pool_exhaustion_is_a_clean_failure(pg_url: str) -> None:
    data = infra_data()
    data["vip_pools"][-1]["ranges"] = [{"start": "10.30.0.10", "end": "10.30.0.10"}]
    async with world(pg_url, data) as w:
        pve = FakeProxmox()
        with pytest.raises(DeployError, match="no free address"):
            await InfraService(w.db, pve).deploy_se_group(request(2))
        assert pve.vms == {}


async def test_jobs_run_through_the_worker_and_need_tenant_admin(pg_url: str) -> None:
    from switchyard.domain.workflow import Role
    from switchyard.services.errors import PermissionDeniedError

    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        jobs = InfraJobs(w.db)
        with pytest.raises(PermissionDeniedError):
            await jobs.request_sdn_apply(principal(Role.OPERATOR), TENANT)
        await jobs.request_sdn_apply(principal(), TENANT)
        await jobs.request_deploy(principal(), TENANT, request(1))
        handlers = build_handlers(w.db, w.pipeline, None, infra=InfraService(w.db, pve))
        worker = Worker(w.db.admin, handlers, worker_id="t")
        while await worker.run_once():
            pass
        assert pve.sdn_applied == 1 and len(pve.vms) == 1
        assert (
            await count(
                w, f"SELECT count(*) FROM job WHERE kind='{APPLY_SDN_JOB}' AND state='succeeded'"
            )
            == 1
        )


async def engine_ids(w: World) -> dict[str, UUID]:
    async with admin_session(w.db.admin) as s:
        rows = (await s.execute(text("SELECT name, id FROM service_engine"))).all()
    return dict(rows)


async def test_retire_destroys_the_vm_and_removes_the_row(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        infra = InfraService(w.db, pve)
        await infra.deploy_se_group(request(2))
        ids = await engine_ids(w)
        result = await infra.retire_engine(TENANT, ids["edge-2"])
        assert result == {"retired": "edge-2", "vm_destroyed": True}
        assert [s.name for s, _ in pve.vms.values()] == ["edge-1"]
        assert set(await engine_ids(w)) == {"edge-1"}
        assert await count(w, "SELECT count(*) FROM audit_event WHERE action='engine.retired'") == 1


async def test_the_last_serving_engine_cannot_be_retired(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        infra = InfraService(w.db, pve)
        await infra.deploy_se_group(request(1))
        only = (await engine_ids(w))["edge-1"]
        with pytest.raises(DeployError, match="only enabled engine"):
            await infra.retire_engine(TENANT, only)
        assert len(pve.vms) == 1
        with pytest.raises(DeployError, match="does not exist"):
            await infra.retire_engine(TENANT, UUID(int=9))


async def test_a_vm_that_cannot_be_destroyed_keeps_the_engine_registered(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        infra = InfraService(w.db, pve)
        await infra.deploy_se_group(request(2))
        victim = (await engine_ids(w))["edge-2"]
        pve.fail_on.add("destroy_vm")
        with pytest.raises(DeployError, match="could not destroy"):
            await infra.retire_engine(TENANT, victim)
        assert await count(w, "SELECT count(*) FROM service_engine") == 2
        assert (
            await count(
                w, f"SELECT count(*) FROM service_engine WHERE id='{victim}' AND state='disabled'"
            )
            == 1
        ), "it stays out of rollouts until the retire is retried"
        await infra.retire_engine(TENANT, victim)
        assert await count(w, "SELECT count(*) FROM service_engine") == 1


async def test_retire_job_is_queued_by_tenant_admins(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        infra = InfraService(w.db, pve)
        await infra.deploy_se_group(request(2))
        victim = (await engine_ids(w))["edge-2"]
        from switchyard.domain.workflow import Role
        from switchyard.services.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await InfraJobs(w.db).request_retire(principal(Role.OPERATOR), TENANT, victim)
        await InfraJobs(w.db).request_retire(principal(), TENANT, victim)
        handlers = build_handlers(w.db, w.pipeline, None, infra=infra)
        worker = Worker(w.db.admin, handlers, worker_id="t")
        while await worker.run_once():
            pass
        assert len(pve.vms) == 1


async def test_deploy_addresses_the_data_nics_without_reusing_addresses(pg_url: str) -> None:
    async with world(pg_url, with_nic_pools(infra_data())) as w:
        pve = FakeProxmox()
        infra = InfraService(w.db, pve)
        backend = uuid.UUID(symbolic_uuid("net-be"))
        pools = {backend: uuid.UUID(symbolic_uuid("vp-backend-se"))}
        await infra.deploy_se_group(request(2, nic_pools=pools))
        specs = [s for s, _ in pve.vms.values()]
        assert [s.nic_addresses for s in specs] == [
            (None, "10.20.0.200/24"),
            (None, "10.20.0.201/24"),
        ]
        assert (
            await count(w, "SELECT count(*) FROM service_engine WHERE data_ips <> '[]'::jsonb") == 2
        )
        listed = await EngineService(w.db).describe(principal(), TENANT, se_group_id=GROUP)
        assert [e.data_ips for e in listed] == [
            ((str(backend), "10.20.0.200"),),
            ((str(backend), "10.20.0.201"),),
        ]
        await infra.deploy_se_group(
            request(3, nic_pools=pools)
        )  # one more engine continues the range
        assert [s.nic_addresses for s, _ in pve.vms.values()][-1] == (None, "10.20.0.202/24")


async def test_deploy_rejects_a_nic_pool_on_the_wrong_network(pg_url: str) -> None:
    async with world(pg_url, with_nic_pools(infra_data())) as w:
        wrong = {uuid.UUID(symbolic_uuid("net-lb")): uuid.UUID(symbolic_uuid("vp-backend-se"))}
        with pytest.raises(DeployError):
            await InfraService(w.db, FakeProxmox()).deploy_se_group(request(1, nic_pools=wrong))


async def test_two_groups_on_one_network_never_share_an_address(pg_url: str) -> None:
    d = infra_data()
    second = copy.deepcopy(d["se_groups"][0])
    second.update(id="sg-second", name="second")
    d["se_groups"].append(second)
    async with world(pg_url, d) as w:
        pve = FakeProxmox()
        infra = InfraService(w.db, pve)
        await infra.deploy_se_group(request(1))
        await infra.deploy_se_group(request(1, se_group_id=uuid.UUID(symbolic_uuid("sg-second"))))
        addresses = [s.mgmt_ip_cidr for s, _ in pve.vms.values()]
        assert len(addresses) == 2 and len(set(addresses)) == 2, addresses


async def test_an_engine_never_gets_the_address_of_a_service_vip(pg_url: str) -> None:
    d = infra_data()
    d["pools"].append(
        {
            "id": "tcp-pool",
            "tenant_id": "t-acme",
            "name": "tcp-pool",
            "members": [{"address": "10.20.0.60", "port": 9000}],
        }
    )
    for i, vip in enumerate(("192.0.2.10", "192.0.2.11")):  # the first two addresses of the pool
        d["virtual_services"].append(
            {
                "id": f"vs-edge{i}",
                "tenant_id": "t-acme",
                "se_group_id": "seg-1",
                "name": f"edge{i}",
                "protocol": "tcp",
                "vip": vip,
                "ports": [{"port": 9000 + i}],
                "default_pool_id": "tcp-pool",
            }
        )
    async with world(pg_url, d) as w:
        pve = FakeProxmox()
        lb = uuid.UUID(symbolic_uuid("net-lb"))
        pools = {lb: uuid.UUID(symbolic_uuid("vp-main"))}
        await InfraService(w.db, pve).deploy_se_group(request(1, nic_pools=pools))
        (spec,) = [s for s, _ in pve.vms.values()]
        assert spec.nic_addresses[0] not in ("192.0.2.10/24", "192.0.2.11/24")
        assert spec.nic_addresses[0] == "192.0.2.12/24"


def test_node_names_cannot_change_a_proxmox_path() -> None:
    from switchyard.services.infra import validate_node_name

    assert validate_node_name("pve-atl-01") == "pve-atl-01"
    for bad in ("../cluster", "pve1/qemu/5", "a?b", "", "-x", "a" * 64, "pve 1"):
        with pytest.raises(ValueError):
            validate_node_name(bad)
    payload = request(1).as_payload() | {"nodes": ["../cluster"]}
    with pytest.raises(ValueError):
        DeployRequest.from_payload(payload)


async def test_only_templates_can_be_cloned_by_a_deploy(pg_url: str) -> None:
    async with world(pg_url, infra_data()) as w:
        pve = FakeProxmox()
        pve.templates = {9000}
        with pytest.raises(DeployError, match="not a Proxmox template"):
            await InfraService(w.db, pve).deploy_se_group(request(1, template_vmid=123))
        assert not pve.vms

"""SDN apply and SE group deployment against the fake Proxmox."""

import copy
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from switchyard.adapters.proxmox import FakeProxmox
from switchyard.db.tenancy import admin_session
from switchyard.domain.loader import symbolic_uuid
from switchyard.jobs.handlers import build_handlers
from switchyard.jobs.worker import Worker
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
        assert (
            await count(
                w, "SELECT count(*) FROM service_engine WHERE agent_url = '10.30.0.10:7443'"
            )
            == 1
        )


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

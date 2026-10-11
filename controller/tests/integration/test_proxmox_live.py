"""HttpProxmox against a real Proxmox VE API (deploy/lab/pve: github.com/dockur/proxmox).

Skipped unless ``SWITCHYARD_TEST_PVE_URL``, ``SWITCHYARD_TEST_PVE_TOKEN_ID`` and
``SWITCHYARD_TEST_PVE_SECRET`` are set; ``sh deploy/lab/pve/bootstrap.sh`` prints them. Without
``/dev/kvm`` the template is created with ``kvm=0`` so VMs start under TCG.
"""

import os
from collections.abc import AsyncIterator

import pytest

from switchyard.adapters.proxmox import HttpProxmox, SdnSubnet, VmSpec

URL = os.environ.get("SWITCHYARD_TEST_PVE_URL")
TOKEN = os.environ.get("SWITCHYARD_TEST_PVE_TOKEN_ID")
SECRET = os.environ.get("SWITCHYARD_TEST_PVE_SECRET")
TEMPLATE = 9900

pytestmark = pytest.mark.skipif(
    not (URL and TOKEN and SECRET), reason="needs a Proxmox lab: see deploy/lab/pve"
)


@pytest.fixture
async def pve() -> AsyncIterator[HttpProxmox]:
    assert URL and TOKEN and SECRET
    client = HttpProxmox(URL, TOKEN, SECRET, verify=False, task_timeout_s=240)
    try:
        yield client
    finally:
        await client.close()


async def test_nodes_and_storage_are_reported(pve: HttpProxmox) -> None:
    nodes = await pve.list_nodes()
    assert nodes and nodes[0].online
    storages = await pve.list_storage(nodes[0].name)
    assert any(s.name == "local" for s in storages)


async def test_sdn_zone_vnet_subnet_lifecycle(pve: HttpProxmox) -> None:
    assert await pve.ensure_zone("sytest", kind="simple") is True
    assert await pve.ensure_zone("sytest", kind="simple") is False  # idempotent
    try:
        assert await pve.ensure_vnet("sytvnet", "sytest") is True
        await pve.ensure_subnet("sytvnet", SdnSubnet("192.0.2.0/24", "192.0.2.1"))
        await pve.apply_sdn()
        assert await pve.zone_vnets("sytest") == ["sytvnet"]
    finally:
        await pve.delete_vnet("sytvnet")
        await pve.delete_zone("sytest")
        await pve.apply_sdn()
    assert await pve.zone_vnets("sytest") == []


async def test_clone_configure_start_stop_destroy(pve: HttpProxmox) -> None:
    node = (await pve.list_nodes())[0].name
    # A diskless template with kvm=0, so the lab works without nested virtualization.
    if await pve.get_vm(node, TEMPLATE) is None:
        create = await pve._call(
            "POST",
            f"/nodes/{node}/qemu",
            {
                "vmid": TEMPLATE,
                "name": "sy-lab-template",
                "memory": 128,
                "cores": 1,
                "kvm": 0,
                "net0": "virtio,bridge=vmbr0",
                "ostype": "l26",
            },
        )
        await pve._task(node, create)
        await pve._task(node, await pve._call("POST", f"/nodes/{node}/qemu/{TEMPLATE}/template"))
    info = await pve.create_vm(
        VmSpec(
            name="sy-lab-se",
            node=node,
            template_vmid=TEMPLATE,
            mgmt_ip_cidr="10.9.9.5/24",
            gateway="10.9.9.1",
            cores=1,
            memory_mb=128,
        )
    )
    try:
        assert (await pve.get_vm(node, info.vmid)).status == "stopped"  # type: ignore[union-attr]
        await pve.start_vm(node, info.vmid)
        assert (await pve.get_vm(node, info.vmid)).status == "running"  # type: ignore[union-attr]
        await pve.stop_vm(node, info.vmid)
        assert (await pve.get_vm(node, info.vmid)).status == "stopped"  # type: ignore[union-attr]
    finally:
        await pve.destroy_vm(node, info.vmid)
    assert await pve.get_vm(node, info.vmid) is None

"""Proxmox VE adapter: SDN objects and SE virtual machines.

Only workers use it (backend design, "Adapters"). The API token comes from the secrets backend and
should carry a role limited to the SE pool and the SDN zones the controller manages.

Two implementations share :class:`Proxmox`:

- :class:`HttpProxmox`: the real REST client (``/api2/json``), waiting for Proxmox tasks (UPIDs).
- :class:`FakeProxmox`: in memory, with failure injection, for tests.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class ProxmoxError(Exception):
    """A Proxmox call failed."""


@dataclass(frozen=True)
class VmSpec:
    """What to create for one service engine.

    Attributes:
        name: VM name (also the hostname).
        node: Proxmox node to place it on.
        template_vmid: VM template to clone.
        mgmt_ip_cidr: Management address in CIDR form, set through cloud-init.
        gateway: Management default gateway.
        pool: Proxmox resource pool, if any.
        mgmt_bridge: Bridge or VNet for the management NIC (``net0``); the template's if ``None``.
        nics: Extra NICs as ``(bridge_or_vnet, vlan_tag or None)``, in order (net1, net2, ...).
        cores: vCPUs.
        memory_mb: RAM.
        ssh_public_keys: Keys for the cloud-init user (operator access only; the controller never
            uses SSH).
        user_data_snippet: Optional cloud-init snippet volume id.
    """

    name: str
    node: str
    template_vmid: int
    mgmt_ip_cidr: str
    gateway: str | None = None
    pool: str | None = None
    mgmt_bridge: str | None = None
    nics: tuple[tuple[str, int | None], ...] = ()
    cores: int = 2
    memory_mb: int = 2048
    ssh_public_keys: str | None = None
    user_data_snippet: str | None = None


@dataclass(frozen=True)
class VmInfo:
    """Observed state of one VM."""

    vmid: int
    node: str
    name: str
    status: str  # running | stopped | ...


@dataclass(frozen=True)
class SdnSubnet:
    """A subnet of a VNet."""

    cidr: str
    gateway: str | None = None


class Proxmox(Protocol):
    """What the controller needs from Proxmox."""

    async def ensure_zone(
        self, zone: str, *, kind: str = "vxlan", peers: str | None = None
    ) -> None:
        """Create the SDN zone if it does not exist."""
        ...

    async def ensure_vnet(self, vnet: str, zone: str, *, tag: int | None = None) -> None:
        """Create the VNet if it does not exist."""
        ...

    async def ensure_subnet(self, vnet: str, subnet: SdnSubnet) -> None:
        """Create the subnet if it does not exist."""
        ...

    async def delete_vnet(self, vnet: str) -> None:
        """Delete a VNet and its subnets (idempotent)."""
        ...

    async def apply_sdn(self) -> None:
        """Apply pending SDN changes cluster-wide."""
        ...

    async def create_vm(self, spec: VmSpec) -> VmInfo:
        """Clone the template, configure it and return it (stopped)."""
        ...

    async def start_vm(self, node: str, vmid: int) -> None:
        """Start a VM."""
        ...

    async def stop_vm(self, node: str, vmid: int) -> None:
        """Stop a VM (idempotent)."""
        ...

    async def destroy_vm(self, node: str, vmid: int) -> None:
        """Stop and destroy a VM (idempotent)."""
        ...

    async def get_vm(self, node: str, vmid: int) -> VmInfo | None:
        """Return the VM, or ``None`` if it does not exist."""
        ...

    async def close(self) -> None:
        """Release connections."""
        ...


Sleep = Callable[[float], Awaitable[None]]


class HttpProxmox:
    """Proxmox VE REST client authenticated with an API token.

    Example:
        >>> HttpProxmox.auth_header("sy@pve!ctl", "s3cr3t")
        'PVEAPIToken=sy@pve!ctl=s3cr3t'
    """

    def __init__(
        self,
        base_url: str,
        token_id: str,
        token_secret: str,
        *,
        verify: bool | str = True,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        task_timeout_s: float = 300.0,
        poll_s: float = 1.0,
    ) -> None:
        """Create a client for ``base_url`` (for example ``https://pve1:8006``)."""
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api2/json",
            headers={"Authorization": self.auth_header(token_id, token_secret)},
            verify=verify,
            transport=transport,
            timeout=30.0,
        )
        self._sleep = sleep
        self._timeout = task_timeout_s
        self._poll = poll_s

    @staticmethod
    def auth_header(token_id: str, secret: str) -> str:
        """Return the ``Authorization`` header value."""
        return f"PVEAPIToken={token_id}={secret}"

    async def _call(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        ok_missing: bool = False,
    ) -> Any:
        try:
            response = await self._http.request(method, path, data=data)
        except httpx.HTTPError as exc:
            raise ProxmoxError(f"{method} {path}: {exc}") from exc
        if response.status_code == 404 and ok_missing:
            return None
        if response.status_code >= 400:
            raise ProxmoxError(
                f"{method} {path}: HTTP {response.status_code}: {response.text[:300]}"
            )
        return response.json().get("data")

    async def _task(self, node: str, upid: Any) -> None:
        if not isinstance(upid, str):
            return
        waited = 0.0
        while True:
            status = await self._call("GET", f"/nodes/{node}/tasks/{upid}/status")
            if status and status.get("status") == "stopped":
                if status.get("exitstatus") != "OK":
                    raise ProxmoxError(f"task {upid} failed: {status.get('exitstatus')}")
                return
            if waited >= self._timeout:
                raise ProxmoxError(f"task {upid} did not finish within {self._timeout:.0f}s")
            await self._sleep(self._poll)
            waited += self._poll

    async def _exists(self, path: str) -> bool:
        return await self._call("GET", path, ok_missing=True) is not None

    async def ensure_zone(
        self, zone: str, *, kind: str = "vxlan", peers: str | None = None
    ) -> None:
        """Create the SDN zone if missing."""
        if await self._exists(f"/cluster/sdn/zones/{zone}"):
            return
        data = {"zone": zone, "type": kind}
        if peers:
            data["peers"] = peers
        await self._call("POST", "/cluster/sdn/zones", data)

    async def ensure_vnet(self, vnet: str, zone: str, *, tag: int | None = None) -> None:
        """Create the VNet if missing."""
        if await self._exists(f"/cluster/sdn/vnets/{vnet}"):
            return
        data: dict[str, Any] = {"vnet": vnet, "zone": zone}
        if tag is not None:
            data["tag"] = tag
        await self._call("POST", "/cluster/sdn/vnets", data)

    async def ensure_subnet(self, vnet: str, subnet: SdnSubnet) -> None:
        """Create the subnet if missing."""
        existing = await self._call("GET", f"/cluster/sdn/vnets/{vnet}/subnets") or []
        if any(s.get("cidr") == subnet.cidr for s in existing):
            return
        data: dict[str, Any] = {"subnet": subnet.cidr, "type": "subnet"}
        if subnet.gateway:
            data["gateway"] = subnet.gateway
        await self._call("POST", f"/cluster/sdn/vnets/{vnet}/subnets", data)

    async def delete_vnet(self, vnet: str) -> None:
        """Delete the VNet and its subnets."""
        subnets = await self._call("GET", f"/cluster/sdn/vnets/{vnet}/subnets", ok_missing=True)
        for s in subnets or []:
            await self._call("DELETE", f"/cluster/sdn/vnets/{vnet}/subnets/{s['id']}")
        await self._call("DELETE", f"/cluster/sdn/vnets/{vnet}", ok_missing=True)

    async def apply_sdn(self) -> None:
        """Apply pending SDN configuration."""
        upid = await self._call("PUT", "/cluster/sdn")
        if isinstance(upid, str) and upid.startswith("UPID:"):
            await self._task(upid.split(":")[1], upid)

    async def create_vm(self, spec: VmSpec) -> VmInfo:
        """Clone ``spec.template_vmid`` and configure the clone."""
        vmid = int(await self._call("GET", "/cluster/nextid"))
        clone: dict[str, Any] = {"newid": vmid, "name": spec.name, "target": spec.node, "full": 1}
        if spec.pool:
            clone["pool"] = spec.pool
        path = f"/nodes/{spec.node}/qemu/{spec.template_vmid}/clone"
        await self._task(spec.node, await self._call("POST", path, clone))
        gateway = f",gw={spec.gateway}" if spec.gateway else ""
        config: dict[str, Any] = {
            "cores": spec.cores,
            "memory": spec.memory_mb,
            "ipconfig0": f"ip={spec.mgmt_ip_cidr}{gateway}",
        }
        if spec.mgmt_bridge:
            config["net0"] = f"virtio,bridge={spec.mgmt_bridge}"
        for index, (bridge, tag) in enumerate(spec.nics, start=1):
            config[f"net{index}"] = f"virtio,bridge={bridge}" + (f",tag={tag}" if tag else "")
        if spec.ssh_public_keys:
            config["sshkeys"] = spec.ssh_public_keys
        if spec.user_data_snippet:
            config["cicustom"] = f"user={spec.user_data_snippet}"
        await self._call("PUT", f"/nodes/{spec.node}/qemu/{vmid}/config", config)
        return VmInfo(vmid, spec.node, spec.name, "stopped")

    async def start_vm(self, node: str, vmid: int) -> None:
        """Start a VM and wait for the task."""
        await self._task(node, await self._call("POST", f"/nodes/{node}/qemu/{vmid}/status/start"))

    async def stop_vm(self, node: str, vmid: int) -> None:
        """Stop a VM; a VM that is already gone or stopped is fine."""
        info = await self.get_vm(node, vmid)
        if info is None or info.status == "stopped":
            return
        await self._task(node, await self._call("POST", f"/nodes/{node}/qemu/{vmid}/status/stop"))

    async def destroy_vm(self, node: str, vmid: int) -> None:
        """Stop and destroy a VM; one that does not exist is fine."""
        if await self.get_vm(node, vmid) is None:
            return
        await self.stop_vm(node, vmid)
        path = f"/nodes/{node}/qemu/{vmid}?purge=1&destroy-unreferenced-disks=1"
        await self._task(node, await self._call("DELETE", path))

    async def get_vm(self, node: str, vmid: int) -> VmInfo | None:
        """Return the VM or ``None``."""
        data = await self._call("GET", f"/nodes/{node}/qemu/{vmid}/status/current", ok_missing=True)
        if data is None:
            return None
        return VmInfo(vmid, node, str(data.get("name", "")), str(data.get("status", "unknown")))

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()


@dataclass
class FakeProxmox:
    """In-memory Proxmox for tests, with failure injection.

    Attributes:
        calls: Every operation as ``(name, argument)`` in order.
        fail_on: Operation names that raise :class:`ProxmoxError` once when called.
        sdn_applied: Number of ``apply_sdn`` calls.
    """

    zones: dict[str, str] = field(default_factory=dict)
    vnets: dict[str, tuple[str, int | None]] = field(default_factory=dict)
    subnets: dict[str, list[SdnSubnet]] = field(default_factory=dict)
    vms: dict[int, tuple[VmSpec, str]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)
    sdn_applied: int = 0
    _next: int = 100

    def _do(self, op: str, arg: str = "") -> None:
        self.calls.append((op, arg))
        if op in self.fail_on:
            self.fail_on.discard(op)
            raise ProxmoxError(f"injected failure in {op}")

    async def ensure_zone(
        self, zone: str, *, kind: str = "vxlan", peers: str | None = None
    ) -> None:
        """Record the zone."""
        self._do("ensure_zone", zone)
        self.zones.setdefault(zone, kind)

    async def ensure_vnet(self, vnet: str, zone: str, *, tag: int | None = None) -> None:
        """Record the VNet (its zone must exist)."""
        self._do("ensure_vnet", vnet)
        if zone not in self.zones:
            raise ProxmoxError(f"zone {zone} does not exist")
        self.vnets.setdefault(vnet, (zone, tag))

    async def ensure_subnet(self, vnet: str, subnet: SdnSubnet) -> None:
        """Record the subnet (the VNet must exist)."""
        self._do("ensure_subnet", subnet.cidr)
        if vnet not in self.vnets:
            raise ProxmoxError(f"vnet {vnet} does not exist")
        known = self.subnets.setdefault(vnet, [])
        if subnet not in known:
            known.append(subnet)

    async def delete_vnet(self, vnet: str) -> None:
        """Remove the VNet."""
        self._do("delete_vnet", vnet)
        self.vnets.pop(vnet, None)
        self.subnets.pop(vnet, None)

    async def apply_sdn(self) -> None:
        """Count the apply."""
        self._do("apply_sdn")
        self.sdn_applied += 1

    async def create_vm(self, spec: VmSpec) -> VmInfo:
        """Create a stopped VM with the next free id."""
        self._do("create_vm", spec.name)
        if any(s.name == spec.name for s, _ in self.vms.values()):
            raise ProxmoxError(f"VM name {spec.name} is already in use")
        self._next += 1
        self.vms[self._next] = (spec, "stopped")
        return VmInfo(self._next, spec.node, spec.name, "stopped")

    async def start_vm(self, node: str, vmid: int) -> None:
        """Mark the VM running."""
        self._do("start_vm", str(vmid))
        spec, _ = self.vms[vmid]
        self.vms[vmid] = (spec, "running")

    async def stop_vm(self, node: str, vmid: int) -> None:
        """Mark the VM stopped."""
        self._do("stop_vm", str(vmid))
        if vmid in self.vms:
            self.vms[vmid] = (self.vms[vmid][0], "stopped")

    async def destroy_vm(self, node: str, vmid: int) -> None:
        """Remove the VM."""
        self._do("destroy_vm", str(vmid))
        self.vms.pop(vmid, None)

    async def get_vm(self, node: str, vmid: int) -> VmInfo | None:
        """Return the VM if it exists."""
        if vmid not in self.vms:
            return None
        spec, status = self.vms[vmid]
        return VmInfo(vmid, spec.node, spec.name, status)

    async def close(self) -> None:
        """Nothing to release."""

"""Proxmox VE adapter: SDN objects and SE virtual machines.

Only workers use it (backend design, "Adapters"). The API token comes from the secrets backend and
should carry a role limited to the SE pool and the SDN zones the controller manages.

Two implementations share :class:`Proxmox`:

- :class:`HttpProxmox`: the real REST client (``/api2/json``), waiting for Proxmox tasks (UPIDs).
- :class:`FakeProxmox`: in memory, with failure injection, for tests.
"""

import asyncio
import ipaddress
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
        nic_addresses: Address in CIDR form for each extra NIC, parallel to ``nics`` (``None`` or a
            shorter tuple leaves a NIC unaddressed). No gateway: only the management NIC has one.
        cores: vCPUs.
        memory_mb: RAM.
        ssh_public_keys: Keys for the cloud-init user (operator access only; the controller never
            uses SSH).
        user_data_snippet: Optional cloud-init snippet volume id (QEMU only).
        kind: ``qemu`` (production: cloud-init VMs) or ``lxc`` (labs without KVM: the template is a
            container template; there is no cloud-init, so enrollment is delivered out of band).
    """

    name: str
    node: str
    template_vmid: int
    mgmt_ip_cidr: str
    gateway: str | None = None
    pool: str | None = None
    mgmt_bridge: str | None = None
    nics: tuple[tuple[str, int | None], ...] = ()
    nic_addresses: tuple[str | None, ...] = ()
    cores: int = 2
    memory_mb: int = 2048
    ssh_public_keys: str | None = None
    user_data_snippet: str | None = None
    kind: str = "qemu"


@dataclass(frozen=True)
class VmInfo:
    """Observed state of one VM."""

    vmid: int
    node: str
    name: str
    status: str  # running | stopped | ...


@dataclass(frozen=True)
class NodeInfo:
    """A Proxmox cluster node.

    Attributes:
        name: Node name.
        online: True when the cluster reports the node ``online``.
        cpu: CPU load as a fraction (0 to 1), when the node is online.
        max_cpu: Number of CPUs.
        mem_used: Memory in use, bytes.
        mem_total: Memory installed, bytes.
        uptime_s: Seconds since boot.
    """

    name: str
    online: bool
    cpu: float | None = None
    max_cpu: int | None = None
    mem_used: int | None = None
    mem_total: int | None = None
    uptime_s: int | None = None


@dataclass(frozen=True)
class StorageInfo:
    """A storage as one node sees it (bytes)."""

    node: str
    name: str
    kind: str
    active: bool
    content: tuple[str, ...]
    total: int | None = None
    used: int | None = None
    available: int | None = None


@dataclass(frozen=True)
class SdnSubnet:
    """A subnet of a VNet."""

    cidr: str
    gateway: str | None = None


class Proxmox(Protocol):
    """What the controller needs from Proxmox."""

    async def ensure_zone(
        self, zone: str, *, kind: str = "vxlan", peers: str | None = None
    ) -> bool:
        """Create the SDN zone if it does not exist. Returns True when this call created it."""
        ...

    async def ensure_vnet(self, vnet: str, zone: str, *, tag: int | None = None) -> bool:
        """Create the VNet if it does not exist. Returns True when this call created it."""
        ...

    async def ensure_subnet(self, vnet: str, subnet: SdnSubnet) -> None:
        """Create the subnet if it does not exist."""
        ...

    async def delete_vnet(self, vnet: str) -> None:
        """Delete a VNet and its subnets (idempotent)."""
        ...

    async def delete_zone(self, zone: str) -> None:
        """Delete an SDN zone (idempotent). Fails if VNets still use it."""
        ...

    async def zone_vnets(self, zone: str) -> list[str]:
        """Names of the VNets in a zone."""
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

    async def list_nodes(self) -> list[NodeInfo]:
        """Return the cluster nodes with their state and load."""
        ...

    async def list_storage(self, node: str) -> list[StorageInfo]:
        """Return the storages of one node."""
        ...

    async def close(self) -> None:
        """Release connections."""
        ...


Sleep = Callable[[float], Awaitable[None]]


def _ip_option(cidr: str, gateway: str | None = None) -> str:
    """The Proxmox option text for one address: ip=,gw= for IPv4 and ip6=,gw6= for IPv6.

    Example:
        >>> _ip_option("10.0.0.5/24", "10.0.0.1")
        'ip=10.0.0.5/24,gw=10.0.0.1'
        >>> _ip_option("fd00::5/64", "fd00::1")
        'ip6=fd00::5/64,gw6=fd00::1'
    """
    v6 = ipaddress.ip_interface(cidr).version == 6
    text = f"{'ip6' if v6 else 'ip'}={cidr}"
    if gateway:
        text += f",{'gw6' if v6 else 'gw'}={gateway}"
    return text


def _nic_address(spec: VmSpec, index: int) -> str | None:
    """The CIDR address for extra NIC number ``index`` (1-based), if one was allocated."""
    position = index - 1
    return spec.nic_addresses[position] if position < len(spec.nic_addresses) else None


def _is_missing(response: httpx.Response) -> bool:
    """True when Proxmox says the object does not exist.

    A real Proxmox answers ``HTTP 500`` with a message such as ``Configuration file ... does not
    exist`` or ``sdn zone 'x' does not exist`` instead of ``404`` (found by the live test against
    deploy/lab/pve), so both are accepted.

    Example:
        >>> _is_missing(httpx.Response(404))
        True
        >>> _is_missing(httpx.Response(500, text="internal error"))
        False
    """
    if response.status_code == 404:
        return True
    if response.status_code != 500:
        return False
    text = response.text.lower()
    return "does not exist" in text or "no such" in text


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
        if ok_missing and _is_missing(response):
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
                # "WARNINGS: n" is a finished task that logged warnings (seen on a real PVE 9).
                exit_status = str(status.get("exitstatus"))
                if exit_status != "OK" and not exit_status.startswith("WARNINGS"):
                    raise ProxmoxError(f"task {upid} failed: {exit_status}")
                return
            if waited >= self._timeout:
                raise ProxmoxError(f"task {upid} did not finish within {self._timeout:.0f}s")
            await self._sleep(self._poll)
            waited += self._poll

    async def _exists(self, path: str) -> bool:
        return await self._call("GET", path, ok_missing=True) is not None

    async def ensure_zone(
        self, zone: str, *, kind: str = "vxlan", peers: str | None = None
    ) -> bool:
        """Create the SDN zone if missing. Returns True when created."""
        if await self._exists(f"/cluster/sdn/zones/{zone}"):
            return False
        data = {"zone": zone, "type": kind}
        if peers:
            data["peers"] = peers
        await self._call("POST", "/cluster/sdn/zones", data)
        return True

    async def ensure_vnet(self, vnet: str, zone: str, *, tag: int | None = None) -> bool:
        """Create the VNet if missing. Returns True when created."""
        if await self._exists(f"/cluster/sdn/vnets/{vnet}"):
            return False
        data: dict[str, Any] = {"vnet": vnet, "zone": zone}
        if tag is not None:
            data["tag"] = tag
        await self._call("POST", "/cluster/sdn/vnets", data)
        return True

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

    async def delete_zone(self, zone: str) -> None:
        """Delete the SDN zone (no error if it is already gone)."""
        await self._call("DELETE", f"/cluster/sdn/zones/{zone}", ok_missing=True)

    async def zone_vnets(self, zone: str) -> list[str]:
        """Names of the VNets that belong to ``zone``."""
        vnets = await self._call("GET", "/cluster/sdn/vnets") or []
        return sorted(str(v["vnet"]) for v in vnets if v.get("zone") == zone)

    async def apply_sdn(self) -> None:
        """Apply pending SDN configuration."""
        upid = await self._call("PUT", "/cluster/sdn")
        if isinstance(upid, str) and upid.startswith("UPID:"):
            await self._task(upid.split(":")[1], upid)

    async def create_vm(self, spec: VmSpec) -> VmInfo:
        """Clone ``spec.template_vmid`` and configure the clone (a container for ``lxc``)."""
        lxc = spec.kind == "lxc"
        kind = "lxc" if lxc else "qemu"
        vmid = int(await self._call("GET", "/cluster/nextid"))
        config_path = f"/nodes/{spec.node}/{kind}/{spec.template_vmid}/config"
        template = await self._call("GET", config_path, ok_missing=True)
        if template is None:
            raise ProxmoxError(f"template {spec.template_vmid} does not exist on {spec.node}")
        if str(template.get("template", 0)) != "1":
            # Cloning any other guest would copy another engine's disk, keys and enrollment.
            raise ProxmoxError(f"guest {spec.template_vmid} is not a Proxmox template")
        clone: dict[str, Any] = {"newid": vmid, "target": spec.node, "full": 1}
        clone["hostname" if lxc else "name"] = spec.name
        if spec.pool:
            clone["pool"] = spec.pool
        path = f"/nodes/{spec.node}/{kind}/{spec.template_vmid}/clone"
        await self._task(spec.node, await self._call("POST", path, clone))
        mgmt = _ip_option(spec.mgmt_ip_cidr, spec.gateway)
        config: dict[str, Any] = {"cores": spec.cores, "memory": spec.memory_mb}
        if lxc:
            bridge = spec.mgmt_bridge or "vmbr0"
            config["net0"] = f"name=eth0,bridge={bridge},{mgmt}"
            for index, (nic, tag) in enumerate(spec.nics, start=1):
                address = _nic_address(spec, index)
                config[f"net{index}"] = (
                    f"name=eth{index},bridge={nic}"
                    + (f",tag={tag}" if tag else "")
                    + (f",{_ip_option(address)}" if address else "")
                )
            if spec.ssh_public_keys:
                config["ssh-public-keys"] = spec.ssh_public_keys
        else:
            config["ipconfig0"] = mgmt
            if spec.mgmt_bridge:
                config["net0"] = f"virtio,bridge={spec.mgmt_bridge}"
            for index, (nic, tag) in enumerate(spec.nics, start=1):
                config[f"net{index}"] = f"virtio,bridge={nic}" + (f",tag={tag}" if tag else "")
                if address := _nic_address(spec, index):
                    config[f"ipconfig{index}"] = _ip_option(address)
            if spec.ssh_public_keys:
                config["sshkeys"] = spec.ssh_public_keys
            if spec.user_data_snippet:
                config["cicustom"] = f"user={spec.user_data_snippet}"
        await self._call("PUT", f"/nodes/{spec.node}/{kind}/{vmid}/config", config)
        return VmInfo(vmid, spec.node, spec.name, "stopped")

    async def _kind(self, node: str, vmid: int) -> str | None:
        """``lxc`` or ``qemu`` for an existing guest, ``None`` if neither exists."""
        for kind in ("qemu", "lxc"):
            if await self._call(
                "GET", f"/nodes/{node}/{kind}/{vmid}/status/current", ok_missing=True
            ):
                return kind
        return None

    async def start_vm(self, node: str, vmid: int) -> None:
        """Start a VM and wait for the task."""
        kind = await self._kind(node, vmid) or "qemu"
        await self._task(
            node, await self._call("POST", f"/nodes/{node}/{kind}/{vmid}/status/start")
        )

    async def stop_vm(self, node: str, vmid: int) -> None:
        """Stop a VM; a VM that is already gone or stopped is fine."""
        info = await self.get_vm(node, vmid)
        if info is None or info.status == "stopped":
            return
        kind = await self._kind(node, vmid) or "qemu"
        await self._task(node, await self._call("POST", f"/nodes/{node}/{kind}/{vmid}/status/stop"))

    async def destroy_vm(self, node: str, vmid: int) -> None:
        """Stop and destroy a VM; one that does not exist is fine."""
        if await self.get_vm(node, vmid) is None:
            return
        await self.stop_vm(node, vmid)
        kind = await self._kind(node, vmid) or "qemu"
        path = f"/nodes/{node}/{kind}/{vmid}?purge=1&destroy-unreferenced-disks=1"
        await self._task(node, await self._call("DELETE", path))

    async def get_vm(self, node: str, vmid: int) -> VmInfo | None:
        """Return the VM or ``None``."""
        data = None
        for kind in ("qemu", "lxc"):
            data = await self._call(
                "GET", f"/nodes/{node}/{kind}/{vmid}/status/current", ok_missing=True
            )
            if data is not None:
                break
        if data is None:
            return None
        return VmInfo(vmid, node, str(data.get("name", "")), str(data.get("status", "unknown")))

    async def list_nodes(self) -> list[NodeInfo]:
        """Return the cluster nodes (``GET /nodes``), sorted by name."""
        data = await self._call("GET", "/nodes") or []
        return sorted(
            (
                NodeInfo(
                    name=str(n["node"]),
                    online=n.get("status") == "online",
                    cpu=float(n["cpu"]) if n.get("cpu") is not None else None,
                    max_cpu=int(n["maxcpu"]) if n.get("maxcpu") is not None else None,
                    mem_used=int(n["mem"]) if n.get("mem") is not None else None,
                    mem_total=int(n["maxmem"]) if n.get("maxmem") is not None else None,
                    uptime_s=int(n["uptime"]) if n.get("uptime") is not None else None,
                )
                for n in data
            ),
            key=lambda n: n.name,
        )

    async def list_storage(self, node: str) -> list[StorageInfo]:
        """Return one node's storages (``GET /nodes/{node}/storage``), sorted by name."""
        data = await self._call("GET", f"/nodes/{node}/storage") or []
        return sorted(
            (
                StorageInfo(
                    node=node,
                    name=str(s["storage"]),
                    kind=str(s.get("type", "")),
                    active=bool(s.get("active", 0)),
                    content=tuple(sorted(str(s.get("content", "")).split(",")))
                    if s.get("content")
                    else (),
                    total=int(s["total"]) if s.get("total") is not None else None,
                    used=int(s["used"]) if s.get("used") is not None else None,
                    available=int(s["avail"]) if s.get("avail") is not None else None,
                )
                for s in data
            ),
            key=lambda s: s.name,
        )

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
    nodes: list[NodeInfo] = field(default_factory=list)
    storages: dict[str, list[StorageInfo]] = field(default_factory=dict)
    #: When set, only these guest ids may be cloned (the real adapter checks the template flag).
    templates: set[int] | None = None
    _next: int = 100

    def _do(self, op: str, arg: str = "") -> None:
        self.calls.append((op, arg))
        if op in self.fail_on:
            self.fail_on.discard(op)
            raise ProxmoxError(f"injected failure in {op}")

    async def ensure_zone(
        self, zone: str, *, kind: str = "vxlan", peers: str | None = None
    ) -> bool:
        """Record the zone. Returns True when it did not exist."""
        self._do("ensure_zone", zone)
        if zone in self.zones:
            return False
        self.zones[zone] = kind
        return True

    async def ensure_vnet(self, vnet: str, zone: str, *, tag: int | None = None) -> bool:
        """Record the VNet (its zone must exist). Returns True when it did not exist."""
        self._do("ensure_vnet", vnet)
        if zone not in self.zones:
            raise ProxmoxError(f"zone {zone} does not exist")
        if vnet in self.vnets:
            return False
        self.vnets[vnet] = (zone, tag)
        return True

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

    async def delete_zone(self, zone: str) -> None:
        """Remove the zone; fails while a VNet uses it, like Proxmox."""
        self._do("delete_zone", zone)
        if any(z == zone for z, _ in self.vnets.values()):
            raise ProxmoxError(f"zone {zone} still has vnets")
        self.zones.pop(zone, None)

    async def zone_vnets(self, zone: str) -> list[str]:
        """Names of the VNets in ``zone``."""
        return sorted(v for v, (z, _) in self.vnets.items() if z == zone)

    async def apply_sdn(self) -> None:
        """Count the apply."""
        self._do("apply_sdn")
        self.sdn_applied += 1

    async def create_vm(self, spec: VmSpec) -> VmInfo:
        """Create a stopped VM with the next free id."""
        self._do("create_vm", spec.name)
        if self.templates is not None and spec.template_vmid not in self.templates:
            raise ProxmoxError(f"guest {spec.template_vmid} is not a Proxmox template")
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

    async def list_nodes(self) -> list[NodeInfo]:
        """Return the configured nodes."""
        self._do("list_nodes")
        return sorted(self.nodes, key=lambda n: n.name)

    async def list_storage(self, node: str) -> list[StorageInfo]:
        """Return the configured storages of ``node``."""
        self._do("list_storage", node)
        return sorted(self.storages.get(node, []), key=lambda s: s.name)

    async def get_vm(self, node: str, vmid: int) -> VmInfo | None:
        """Return the VM if it exists."""
        if vmid not in self.vms:
            return None
        spec, status = self.vms[vmid]
        return VmInfo(vmid, spec.node, spec.name, status)

    async def close(self) -> None:
        """Nothing to release."""

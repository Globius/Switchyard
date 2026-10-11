# 0054. Live Proxmox adapter tests with dockur/proxmox

Status: Accepted
Date: 2026-10-11

## Context
The Proxmox adapter was tested only against recorded fixtures and fakes (06 section 2: "end-to-end lab test on
Proxmox: not started"). No Proxmox cluster is available to the development machine.

## Decision
- `deploy/lab/pve/` runs github.com/dockur/proxmox (a privileged container with a real Proxmox VE 9 API) and
  `bootstrap.sh` creates a root API token and prints the `SWITCHYARD_TEST_PVE_*` variables.
- `controller/tests/integration/test_proxmox_live.py` runs `HttpProxmox` against it: nodes and storage, an SDN
  zone/VNet/subnet lifecycle with apply, and clone, configure, start, stop and destroy of a VM and of an LXC container. It skips when the
  variables are not set, so normal runs and CI are unaffected.
- Without `/dev/kvm` (Docker Desktop here) VMs run under TCG, slowly; the test template is diskless with `kvm=0`.
  It proves the API contract, not that a real SE boots.

- **Instances as LXC (labs only).** `VmSpec.kind="lxc"` clones a container template through `/lxc/` with `net0`
  `name=eth0,...` syntax; start, stop, destroy and get find the guest kind themselves, so the adapter protocol is
  unchanged. Production keeps QEMU/cloud-init; there is no cloud-init for containers, so enrollment material
  must be pushed in out of band (`pct push`). The WSL2 kernel behind Docker Desktop has no IPVS, so UDP services
  cannot be tested there; HAProxy, FRR and Keepalived VRRP are fine in a container.

## Consequences
The live runs found two real bugs. First: Proxmox answers a missing VM or SDN object with HTTP 500 and a message
("... does not exist"), not 404, so `get_vm`, `ensure_zone`, `ensure_vnet` and the delete calls failed on a
real server. `_is_missing` now recognises it, with a unit test. Second, a finished task can report
`WARNINGS: n` (a container create did); the adapter treated anything but `OK` as a failure and now accepts it.
The remaining gap is a full SE deploy (template
with a real disk and cloud-init, enrollment, Data Plane API), which needs a cluster with KVM.

## Alternatives considered
Recording more fixtures (would only repeat the old assumptions); a nested-virtualization VM (heavier, and not
available on the Windows 10 host).

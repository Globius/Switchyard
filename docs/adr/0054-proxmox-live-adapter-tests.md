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
  zone/VNet/subnet lifecycle with apply, and clone, configure, start, stop and destroy of a VM. It skips when the
  variables are not set, so normal runs and CI are unaffected.
- Without `/dev/kvm` (Docker Desktop here) VMs run under TCG, slowly; the test template is diskless with `kvm=0`.
  It proves the API contract, not that a real SE boots.

## Consequences
The first live run found a real bug: Proxmox answers a missing VM or SDN object with HTTP 500 and a message
("... does not exist"), not 404, so `get_vm`, `ensure_zone`, `ensure_vnet` and the delete calls failed on a
real server. `_is_missing` now recognises it, with a unit test. The remaining gap is a full SE deploy (template
with a real disk and cloud-init, enrollment, Data Plane API), which needs a cluster with KVM.

## Alternatives considered
Recording more fixtures (would only repeat the old assumptions); a nested-virtualization VM (heavier, and not
available on the Windows 10 host).

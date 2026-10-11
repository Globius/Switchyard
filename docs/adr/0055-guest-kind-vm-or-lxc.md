# 0055. Service engines as VMs or LXC containers

Status: Accepted
Date: 2026-10-11

## Context
SEs have been QEMU VMs cloned from a cloud-init template. Containers are lighter (more engines per host, faster
to start) and are the only guests that run on hosts without KVM, such as the Docker-hosted lab (ADR 0054).

## Decision
- A deploy request carries `guest_kind`: `qemu` (default) or `lxc`. It is a property of the request, not the
  SE group, so a group can mix kinds during a migration and no schema migration is needed. Start, stop, retire and
  status find the guest kind from Proxmox, so nothing else in the controller changes.
- Containers are privileged with `nesting=1`, because FRR, Keepalived and nftables need capabilities an
  unprivileged container lacks. The Proxmox **host** must load `ip_vs`, `ip_vs_rr`, `ip_vs_wrr`, `ip_vs_sh` and
  `nf_conntrack`; a container cannot, so UDP services need those host modules.
- The container template is built by `se-image/lxc/build-template.sh` (on the Proxmox node), which runs the same
  `provision.sh` as the VM image, so both kinds carry identical software.
- Containers have no cloud-init. Management address, gateway and keys are set through the container config; the
  enrollment token, controller URL and CA must be pushed in out of band (`pct push`), as for VMs today (ADR 0011).

- The kind is stored on the engine at deploy (`service_engine.guest_kind`, NULL for hand-built engines, existing deployed
  engines became `qemu`) and returned as `guest_kind` on the engine read, for a VM/CT marker in lists.

## Consequences
Verified in the lab: the template builds, a full clone starts, HAProxy 3.2 answers on the local health port, node
exporter on 9100, Keepalived 2.3.3 and FRR installed (started by the agent, not at boot). Not verified: VRRP/BGP in a
container, IPVS (the lab kernel has none), enrollment, and any production container behavior beyond the lab. Containers
share the host kernel, so a kernel bug or a host module change affects every engine on the node; keep VM engines
for anything that needs kernel isolation. An API token cannot set the `nesting` feature (root@pam only), so a
template made through the API needs it set once with `pct set`.

## Alternatives considered
Making the kind a field of the SE group (needs a migration and changes to every group form; a request field is
enough until someone needs to see the kind later, which `GET` of an engine can show from Proxmox); unprivileged
containers (FRR and Keepalived fail).

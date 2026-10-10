# 0009. Infrastructure jobs: SDN, IPAM and SE group deployment

Status: Accepted

## Context

M4 lets the controller build the infrastructure SE groups run on: Proxmox SDN objects for managed
networks, addresses from pools, and SE VMs cloned from the template.

## Decisions

1. **Workers only.** `apply_sdn` and `deploy_se_group` are jobs. The API queues them (tenant-admin
   role) and returns `202` with a job id: `POST /tenants/{t}/networks/apply-sdn` and
   `POST /tenants/{t}/se-groups/{id}/deploy`.
2. **Proxmox behind an interface.** `adapters/proxmox.py` has a typed `Proxmox` protocol, an
   `HttpProxmox` REST client (API token, UPID task polling) and a `FakeProxmox` with failure injection.
   The token is read from the secrets backend and is never stored in PostgreSQL. It is tested against
   a mocked API; a live-cluster run belongs to the end-to-end lab.
3. **SDN apply is serialized.** Proxmox applies SDN cluster-wide, so every SDN change holds one
   advisory lock. Ensure calls are idempotent. Unmanaged (attached) networks are never touched.
4. **IPAM is a pure function.** `domain/ipam.allocate` returns the lowest usable free address of a pool
   (no network, broadcast or gateway address). Management addresses for new SEs come from a pool on the
   group's `mgmt` network. A VIP is reserved by creating the virtual service in a change set, where the
   existing validation rejects duplicates; there is no separate reservation table.
5. **Deploy is declarative and self-cleaning.** `count` is the total the group should have. Existing
   engines are kept; missing ones are created round-robin over the given nodes, started and registered as
   `service_engine` rows. If a step fails, the VMs and rows created by that run are removed and earlier
   engines are untouched. A deploy holds the SE group's lock so it cannot interleave with an apply.

## Not yet covered

- Removing SDN objects when a network is deleted, and shrinking a group (scale-in), are not implemented.
- Agent enrolment exists (ADR 0011), but `deploy_se_group` does not hand tokens to new VMs yet; that
  belongs to the SE template.
- The SE template itself (`se-image/`) is not built here.

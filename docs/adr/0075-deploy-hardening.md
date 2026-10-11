# 0075. Deploy hardening: templates, node names, engine addresses

Status: Accepted
Date: 2026-10-11

## Context

The second security review (docs/security/backend-review.md, "Second review") looked at the LXC guest kind, data NIC addressing and the
engine address fields. Three problems came out of the deploy request, which a tenant administrator writes.

## Decisions

1. **Only Proxmox templates are cloned.** `template_vmid` is tenant input. Before cloning, the Proxmox adapter reads the guest's config and
   refuses anything whose `template` flag is not set, so a deploy cannot copy another tenant's running engine (its disk, agent key and enrolment).
   The fake adapter has `templates` for tests.
2. **Node and pool names are validated.** A node name is a path segment of every Proxmox call (`/nodes/<node>/...`). The API accepts
   `[A-Za-z0-9-]` up to 63 characters for a node and `[A-Za-z0-9._-]` for the Proxmox pool, and the job re-checks the payload.
3. **Engine addresses and service VIPs do not collide.** The deploy job treats the VIPs of the tenant's services on a network as taken when it gives
   the NICs addresses from a pool, and `GET /vip-pools/{id}/allocations` counts addresses held by engines (management and data NICs) as used and
   lists them in `engine_addresses`, so `next_free` and `POST .../next-free` never offer one.
4. Left as findings (design level): `guest_kind: lxc` is reachable in production (the template decides whether the container is privileged), the
   lab enrolment script leaves one-time tokens in a work directory, and a service VIP can still be typed by hand onto an engine's address
   (the domain model does not know engine addresses).

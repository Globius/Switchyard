# Pool discovery by Proxmox tag

A pool can fill its members from Proxmox guests instead of a typed list (ADR 0145).

```yaml
pools:
  - name: web
    health_monitor_id: ...
    discovery:
      tag: sy-acme.web        # Proxmox tag, must start with sy-<tenant>.
      cidr: 10.20.0.0/24      # a guest's address in this network becomes the member address
      port: 8080
      weight: 100             # of a new member
```

## How it works

1. Tag the guests in Proxmox (`sy-acme.web`). Tags are cluster-wide, which is why the prefix names the tenant.
2. Every minute the worker lists running guests that carry the tag and reads their addresses: QEMU guests through the guest agent,
   containers from their interfaces. The first address inside `cidr` is the member address.
3. If the member list differs, the worker opens a change set `discovery: update members of web`, author `system:discovery`, and
   applies it through the usual canary. With approval required it waits in the approval list.

Members are named `pve-<vmid>`. Their weight and admin state are yours: draining one with the member state endpoint is kept until the
guest goes away.

## Things to know

- Install the QEMU guest agent in the guests and enable it in the VM options; without it the guest has no address and is skipped (an
  existing member is kept).
- A guest that stops or loses the tag leaves the pool at the next run. The last member is never removed this way: a pool that would
  become empty is left as it is, and the worker logs `discovery would empty pool`.
- Members typed into a discovered pool are replaced at the next change. Remove `discovery` to manage the pool by hand again.
- The worker needs the same `SWITCHYARD_PVE_*` settings as the SE deploy jobs; without them the job does not run.
- A tag outside `sy-<tenant>.` is refused when the pool is saved (`discovery_tag_not_allowed`).

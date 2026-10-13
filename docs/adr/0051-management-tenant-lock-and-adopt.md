# 0051. Management tenant lock and the `adopt` import

Status: Accepted
Date: 2026-10-11

## Context
02 ("HA and deployment") puts the controller VMs in a `management` tenant, built first by OpenTofu and
cloud-init and then "adopted", with the tenant locked: changes need a system administrator plus a
confirmation, and the pipeline refuses to remove the controller VIP or its SE group.

## Decision
- **Lock by name.** The tenant named `management` is locked (`switchyard.domain.management`). No schema
  change: the lock reuses the break-glass path of `require_approval`. Applying needs a system
  administrator and a recorded reason, exactly as for a tenant that requires approval (the reason is the
  "confirmation" and lands in the audit log as `change_set.break_glass_apply`).
- **Protected objects.** A virtual service named `controller` in the tenant, and the SE group that serves
  it, cannot be deleted, and the service cannot be disabled. The check runs when operations are added and
  again at apply, so a stale draft cannot slip through. Finding code: `controller_protected`.
- **`syctl adopt FILE`** loads a snapshot YAML (same format as the demo seed) plus an `engines` list,
  creating the tenant, its objects and the engine rows (with Proxmox node and vmid) without deploying
  anything. It refuses a tenant that already exists. An example is `deploy/prod/adopt/management.example.yaml`.

## Consequences
The approval-by-second-user path still works in the management tenant; the lock only removes the
single-person apply. Renaming the tenant or the service removes protection, so those names are part of the
deployment contract (documented in the adoption example). The external ESXi controller member
(06 section 2) still needs its own design, since the adopt file only describes platform objects.

## Update 2026-10-12: the external controller member
The "own design" this ADR left open is decided here, with these defaults.

- **The member is a record, not a platform object.** A table `controller_member` (migration 0041) holds the name (the
  Patroni member name), address, platform (`proxmox`, `esxi` or `other`), a hypervisor host label, `external` (default: every
  platform but Proxmox), `nofailover` and an optional Patroni REST URL. It is system-wide, not tenant data, and only the admin
  role reads it. Nothing is deployed or joined by creating one.
- **Status is read, never written.** The controller asks the first member whose `patroni_url` answers for `GET /cluster` and
  matches members on name: role (leader, sync standby, replica), state, lag, timeline and the `nofailover` tag. A member Patroni
  does not list is shown as "Not in the cluster". Unreachable Patroni leaves the list without roles; it is never an error.
- **Adoption declares it.** `syctl adopt` takes an optional `controller_members` list, so the ESXi VM is described in the same
  file as the rest. Bad entries (loopback or link-local addresses, an unknown platform, a repeated name) refuse the whole file.
- **API and UI.** `GET`/`POST /system/controller/members`, `PATCH`/`DELETE /system/controller/members/{id}` for system
  administrators (the `manage controller` permission). The Admin > Controller page shows the members with an *External* badge for
  the ESXi VM. Patroni's REST port has no authentication, so the client refuses loopback, link-local, multicast and reserved
  targets; private addresses are allowed because the VMs are on private networks.
- **Removal is bookkeeping.** Deleting an entry does not remove the member from etcd or Patroni; the runbook order stays
  (`etcdctl member remove`, `patronictl remove`, then the entry).

## Alternatives considered
A `locked` column on `tenant`: explicit, but needs a migration, API field and UI work in areas other
threads are changing. Revisit if more than one tenant needs locking.

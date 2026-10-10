# 0017. Proxmox SDN cleanup when a managed network is deleted

Status: Accepted

## Context

`apply_sdn` (ADR 0009) created zones, VNets and subnets for managed networks but never removed
them. Deleting a network left its VNet in Proxmox. The Proxmox API's `ensure_*` calls are idempotent,
so after the fact the controller cannot tell an object it created from one that was already there.
Deleting by name alone could remove somebody else's network.

## Decisions

1. **Record what the controller created.** `ensure_zone` and `ensure_vnet` return `True` only when the
   call created the object. `apply_sdn` stores those in the new RLS-protected table `sdn_object`
   (`tenant_id, kind, name`). Pre-existing objects are never recorded, so never deleted. Subnets go with
   their VNet.
2. **`apply_sdn` reconciles.** Under the same cluster-wide SDN lock it deletes recorded VNets no
   managed network uses, then recorded zones no managed network uses. A zone that still holds any VNet
   (for example one created by hand) is kept. One `apply_sdn` to Proxmox follows if anything changed.
   The job result gains `removed` (`vnet:<name>`, `zone:<name>`).
3. **Trigger.** A change-set apply that deletes a network enqueues `apply_sdn`. Attached networks
   (`managed: false`) are never touched, and nothing is deleted while a network still references the
   object.
4. **Not covered.** Networks created before this change have no record, so their SDN objects stay and
   need manual cleanup.

# ADR 0078: IPv6 and dual-stack

Status: Accepted

## Context

The first Proxmox hosts are IPv6-only. An audit found the data model, HAProxy, FRR and IPVS paths already
handle IPv6, with three gaps in the renderers.

## Decision

- Keepalived renders one `vrrp_instance` per address family. VRRP cannot carry both families in one
  instance. The IPv4 instance keeps its name; the IPv6 one is `<name>_6` and uses VRRPv3. Static routes go
  to the instance of their family.
- The BGP router id is the engine's IPv4 management address when it has one, otherwise a value derived
  from the engine name inside `100.64.0.0/10`. It is only an identifier, so nothing needs configuring.
- Monitor test targets bracket IPv6 literals.

## Consequences

Single-stack groups render byte for byte as before. The derived router id must be unique within a BGP
group; collisions among a few engines in 4 million values are negligible. Provisioning (Proxmox adapter,
deploy job, SE image) is handled separately by Operations.

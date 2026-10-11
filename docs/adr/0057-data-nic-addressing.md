# 0057. Addresses for the VIP and backend NICs of a deployed engine

Status: Accepted
Date: 2026-10-11

## Context
`deploy_se_group` cloned the SE template and set only the management address. The VIP and backend NICs were attached to their
networks with no address, so a VRRP engine had no source address for advertisements and no way to reach pool members on
the backend network until someone configured them by hand (found while building the two-engine lab, ADR 0056).

## Decision
- A deploy request may carry `nic_pools`: for each VIP or backend network the group attaches, the address pool (an
  existing pool on that network) engines draw from. Each new engine gets one address per NIC, in the network's prefix length,
  with no gateway (only management has one). Networks without an entry keep the old behaviour (an unaddressed NIC), so
  existing clients keep working.
- QEMU engines get `ipconfig<n>` (cloud-init), containers get `ip=` on `net<n>`.
- Addresses are recorded on the engine (`service_engine.data_ips`, migration 0021) and the allocator reads the addresses of
  **every** engine of the tenant, not only the group's. The same query fixed a related bug: two SE groups sharing a management
  network were each handed `.100` first.
- These pools are not the groups' VIP pools. Keep engine addresses and VIPs in separate ranges (the lab uses
  `10.99.0.100-119` and `10.99.0.230-250` on one network).

## Consequences
Nothing yet releases an address except retiring the engine (the row, and so the record, goes). Hand-registered engines
keep their own addressing. A routing profile's VRRP `interface` must name the NIC of the VIP network (`eth1` in the lab layout).

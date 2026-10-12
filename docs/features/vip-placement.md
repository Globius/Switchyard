# VIP placement: VRRP and BGP

**What.** How a VIP reaches the engines of an SE group. `ha_mode: vrrp` (Keepalived; one engine holds the VIP, another takes
over) or `ha_mode: bgp` (FRR; every healthy engine announces the VIP and the upstream router spreads traffic with ECMP). The
details live in a **routing profile** referenced by the group.

**How to use.**

```yaml
kind: bgp
name: core
asn: 65010
peers: [{peer_ip: 10.0.0.1, remote_asn: 65000, md5_ref: kv/tenants/acme/bgp/core}]
bfd: true
max_prefixes: 10
```

- The agent's watchdog withdraws a VIP when HAProxy (or, for UDP, Keepalived) on that engine stops serving it, and announces
  it again when it recovers.
- IPv6 and dual stack work in both modes (one VRRP instance per address family, VRRPv3 for IPv6).
- Static routes in the routing profile cover networks that are not directly attached.

**Limits.** Engines accept no routes from their neighbours; `max_prefixes` is a guard against a broken upstream, not a table
size. VRRP failover takes a few seconds (4 to 5 s in the lab); BGP failover depends on BFD and timers. The lab results are in
[docs/lab](../lab/2026-10-12-full-pass.md).

**Decisions:** [0008](../adr/0008-agent-routing-and-status.md), [0065](../adr/0065-bgp-prefix-limits.md),
[0078](../adr/0078-ipv6-and-dual-stack.md).

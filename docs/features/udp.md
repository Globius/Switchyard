# UDP services

**What.** `protocol: udp` services are served by Keepalived and IPVS in NAT mode with source NAT on the engine, so backends
need no change. Health checks are `udp` monitors.

**How to use.** A UDP service takes a name, the SE group, a VIP, ports and `default_pool_id`. Pools use `roundrobin`,
`leastconn` or `source` (IPVS `rr`, `lc`, `sh`; the weighted variants when member weights differ).

```yaml
name: dns
protocol: udp
se_group_id: <group id>
vip: 192.0.2.53
ports: [{port: 53}]
default_pool_id: <pool id>
```

In VRRP mode Keepalived holds the VIP. In BGP mode FRR announces it, and the agent withdraws the route when Keepalived is
down or the service has no healthy real server.

**Limits.** Every HTTP, TLS or policy field is refused with a field error, and so are `random`, PROXY protocol and backup
members. There is no connection-state sync between engines, so a failover resets UDP flows. No protocol-aware probes (DNS,
RADIUS) yet.

**Decisions:** [0013](../adr/0013-udp-keepalived-ipvs.md), [0020](../adr/0020-udp-m6-implementation.md).

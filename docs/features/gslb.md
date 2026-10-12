# GSLB

**What.** DNS-based global load balancing on PowerDNS (LUA records) behind dnsdist. A tenant's **wide IPs** live in **zones**
a system administrator assigned to it; each wide IP answers with healthy members by `failover`, `all`, `weighted`, `subnet`,
`geo` or `latency`. A member is an address or a virtual service (its VIP is followed when it moves).

**How to use.** `POST .../gslb-zones/{zone}/wide-ips` with members, algorithm and a `tcp` or `http` health check. Wide IPs
change live (no change set) and are audited. `GET .../gslb/status` compares each wide IP with PowerDNS; `POST .../gslb/sync`
rewrites on request. DNSSEC per zone: `POST .../gslb-zones/{zone}/dnssec`, then publish the DS records from `.../ds`. The full
guide: [GSLB](../gslb.md).

**Limits.** Off unless the operator configures PowerDNS. Health is checked by PowerDNS from its own address; per-member health
is not in the API. Delegation (NS and DS at the parent) and key rollover are manual. `geo` needs a GeoIP database on the
PowerDNS server. No UI screens yet. Several vantage points and remote sites are only proposed (ADR 0180).

**Decisions:** [0160](../adr/0160-gslb-on-powerdns.md) to [0165](../adr/0165-gslb-latency-selection.md).

# 0165. GSLB latency-based selection

Status: Accepted
Date: 2026-10-12

## Context
08 asks for latency-based GSLB. PowerDNS has no round-trip measurement, and a DNS answer cannot be tailored to a client's actual
path latency without measuring from the client's side (which F5 and Avi do with probes between sites and client resolvers).

## Decision
The `latency` algorithm answers with the member that has the lowest **measured round-trip time from the controller**, and keeps
the others as backup tiers, health-checked by PowerDNS as for `failover`:
- The job `probe_gslb` (every minute, leader-scheduled) opens three TCP connections to each member's health port, takes the
  median, and smooths it with the previous value (weight 0.5 on the new sample). Results are stored per wide IP
  (`gslb_wide_ip.measurements`: milliseconds by member and the resulting order). An unreachable member sorts last.
- **Hysteresis**: the current first choice keeps its place unless it is unreachable or the best member is at least 25 % faster
  (`LATENCY_MARGIN`), so two similar members do not trade places on every sample.
- The order becomes `ifportup` tiers (rank 1 first). The records are rewritten only when the order changes them. Until the
  first measurement the members form one tier.

## Consequences
The measurement answers "which member is closest to the controller", not "to this client". It fits a controller placed near the
DNS servers and clients (one region, or a controller per region with its own wide IPs) and members that differ in distance or load.
For clients spread over several regions use `geo` (PowerDNS GeoIP) or `subnet`. Probing is a TCP connect to the health port, so a
member's application latency is not measured; an `http` health check still decides up or down. Probe traffic is one connect burst per
member per minute.

## Alternatives considered
Probes from each site's DNS server (needs an agent beside PowerDNS and a way to feed results back); EDNS client-subnet with
a latency map (needs client-side measurements); using HAProxy response times from the telemetry pipeline (measures service speed
at the site, not the path to the client).

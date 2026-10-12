# 0164. dnsdist in front of PowerDNS

Status: Accepted
Date: 2026-10-12

## Context
GSLB makes an authoritative DNS server part of the product, reachable from the internet. An open authoritative server is a
favourite amplification reflector (ANY, large answers, spoofed sources) and a target of query floods, which would take every
tenant's names down together. 08 lists "DNS firewall / rate limit (dnsdist)" as the answer.

## Decision
`deploy/dns/` runs **dnsdist 1.9** in the same network namespace as PowerDNS (`network_mode: service:pdns`; dnsdist takes backend
addresses, not names). dnsdist answers on port 53 and forwards to PowerDNS on a private port (5300); only dnsdist is published.
`deploy/dns/dnsdist.conf` is Lua read from environment variables, so there is nothing to template:
- `ANY` queries, `AXFR` and `IXFR` are answered `REFUSED`, and non-query opcodes `NOTIMP`. Zone transfers to secondaries use the
  private port between your own servers, not the public one.
- A per-client-network rate limit (IPv4 /24, IPv6 /64; `DNSDIST_QPS_LIMIT` 50, `DNSDIST_QPS_BURST` 100): over it, queries are
  **dropped**, not refused, so a spoofed source cannot use the server to send answers to a victim.
- Dynamic blocks: a network above four times the limit for ten seconds, or producing many `REFUSED`, is dropped entirely for
  `DNSDIST_BLOCK_SECONDS` (60).
- A self-generated answer size cap (1232 bytes) so big UDP answers are truncated, not fragmented.

## Consequences
Tested against real PowerDNS and dnsdist (`tests/integration/test_dnsdist_stack.py`): normal questions pass, ANY/AXFR/IXFR are
refused, a flood is mostly dropped. The limits are per client network, so a large resolver shared by many users can hit a limit
meant for one client: raise `DNSDIST_QPS_LIMIT` for a site that sees big resolvers. dnsdist adds a hop (microseconds). The
controller still talks to PowerDNS's API directly, never through dnsdist. DNS-over-TLS/HTTPS, DNSSEC-aware rules and per-tenant
limits are not configured.

## Alternatives considered
Rate limiting in PowerDNS (it has no per-source limiter); firewall rules only (no DNS awareness: cannot tell ANY from A); a
managed DNS DDoS service (not self-hosted).

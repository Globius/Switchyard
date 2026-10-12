# 0163. GSLB: what is built, what is not

Status: Accepted
Date: 2026-10-12

## Decision
Built: zones and wide IPs with `failover`, `all`, `weighted`, `subnet` and `geo`; tcp and http health; virtual-service
members that follow VIP moves; atomic writes; drift status and sync; DNSSEC signing with DS records; a PowerDNS deployment
(`deploy/dns/`); API, audit events and docs. Checked against a real PowerDNS 5 for everything except `geo`.

Not built, on purpose:
- **dnsdist** was deferred here and is now built: [ADR 0164](0164-dnsdist-in-front-of-powerdns.md).
- **Latency-based selection** was deferred here and is now built ([ADR 0165](0165-gslb-latency-selection.md)); **geo without a GeoIP
  database** (the operator installs one: `geoip-database-files` in PowerDNS).
- **Health visibility in the API.** PowerDNS does not expose per-target health; `status` is about records, not members. A
  per-member view would need PowerDNS's `/metrics` or a probe from the controller.
- **UI.** The API and the audit trail are the interface for now; wide IPs and zones need artboards (flagged in
  docs/design/07-ui-vs-artboards.md).
- **Parent-zone automation** (publishing NS and DS in the parent) and **key rollover**.
- **A second PowerDNS as a managed secondary**: operators run secondaries themselves (docs/gslb.md).

## Consequences
GSLB is usable end to end for failover, multivalue and weighted answers with real health checks, which is the case 08 calls
out first (cross-site failover), and the model leaves room for the rest without changing the stored objects.

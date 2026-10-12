# 0162. GSLB zones: who owns a name, DNSSEC, and several sites

Status: Accepted
Date: 2026-10-12

## Decision
- **A zone belongs to one tenant and is created by a system administrator.** The zone name is unique across tenants, and
  may not be a parent or a child of another tenant's zone, so nobody can shadow another tenant's names. The tenant manages
  the wide IPs inside it; it cannot create zones, so it cannot claim `example.com` by asking. Delegating the zone at its
  parent (NS records) is the operator's step; the API answers with the nameservers it was given.
- **DNSSEC** is switched on per zone by a tenant administrator (`POST /tenants/{t}/gslb-zones/{zone}/dnssec`). PowerDNS
  creates and rolls the keys; the controller returns the DS records, which the tenant publishes at the parent
  (`GET .../ds`). The controller does not manage the parent zone and does not rotate keys.
- **Several sites.** Run one PowerDNS per site (`deploy/dns/`), each answering the zone. The controller writes to the one
  named by `SWITCHYARD_POWERDNS_URL`; make the others secondaries (AXFR/IXFR, `deploy/dns/` README in docs/gslb.md), so every
  site answers with the same records and each one health-checks from its own vantage point.
  Health data therefore reflects reachability from each site's DNS server, which is what a client of that site gets.
- Deleting a zone removes it from PowerDNS and deletes its wide IPs.

## Consequences
LUA health state and answers are per PowerDNS server: two sites can briefly answer differently. Secondaries receive the
LUA records through zone transfer (they are a record type like any other) and evaluate them locally; this was not
tried in a multi-server setup here, so the lab runbook has the check to run before relying on it.

## Alternatives considered
Tenant-created zones with a verification step (a DNS challenge per zone: more moving parts than "an administrator hands
over a zone"); a PowerDNS per tenant (isolation is stronger, the operating cost is far higher).

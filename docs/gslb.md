# GSLB: wide IPs on PowerDNS

A **wide IP** is a DNS name that answers with the addresses of whichever of its members are healthy, usually the VIPs of one
service at several sites. PowerDNS answers and checks health; the controller decides what the records say and writes them
through PowerDNS's API. Decisions: [ADR 0160](adr/0160-gslb-on-powerdns.md) (architecture),
[0161](adr/0161-gslb-algorithms-and-record-safety.md) (algorithms, safety),
[0162](adr/0162-gslb-zones-ownership-and-dnssec.md) (zones, DNSSEC, sites),
[0163](adr/0163-gslb-scope-and-deferred.md) (what is not built).

## Set up

1. Run PowerDNS Authoritative with LUA records: `deploy/dns/` (`docker compose --env-file dns.env up -d`). Keep the API
   (port 8081) on the management network, allow only the controller nodes (`PDNS_API_ALLOW_FROM`), and run one per site.
2. On every controller node set `SWITCHYARD_POWERDNS_URL=http://<pdns>:8081` and `SWITCHYARD_POWERDNS_API_KEY`; restart the API
   and the worker (the worker runs the `sync_gslb` job every 5 minutes).
3. Delegate a zone to your nameservers at its parent (NS records), then, as a system administrator, hand it to a tenant:

```sh
syctl api POST /tenants/acme/gslb-zones '{"name":"gslb.example.com","nameservers":["ns1.example.com","ns2.example.com"]}'
```

A zone belongs to one tenant, may not overlap another tenant's zone, and only a system administrator creates it.

## Wide IPs

```sh
syctl api POST /tenants/acme/gslb-zones/gslb.example.com/wide-ips '{
  "name": "shop", "algorithm": "failover", "ttl": 30,
  "health": {"kind": "http", "port": 443, "path": "/healthz", "match": "ok"},
  "members": [
    {"virtual_service": "shop", "site": "pve1", "priority": 1},
    {"address": "198.51.100.20", "site": "pve2", "priority": 2}
  ]}'
```

`shop.gslb.example.com` now answers with pve1's VIP while it is up and with pve2's when it is not; point your public name at it
with a CNAME. `name: "@"` publishes at the zone apex.

| `algorithm` | Answers with | Member fields used |
| --- | --- | --- |
| `failover` | the members of the first `priority` tier that has any up (1 is first) | `priority` |
| `all` | every member that is up (multivalue) | |
| `weighted` | one member that is up, picked by `weight` (0 is never answered); all members if none is up | `weight` |
| `subnet` | the members whose `subnets` contain the client's address; members without `subnets` are the default | `subnets` |
| `geo` | the member closest to the client (PowerDNS needs a GeoIP database) | |
| `latency` | the member with the lowest round trip measured from the controller, the rest as backup; changes only when another is 25 % faster ([ADR 0165](adr/0165-gslb-latency-selection.md)) | |

A member is an `address` (IPv4 or IPv6) or a `virtual_service` (its VIP is followed when it moves; the sync job rewrites the
record, no more than five minutes later). `site` is a free label. IPv4 and IPv6 members answer `A` and `AAAA` from the same name.

**Health** (`health`): `tcp` connects to `port` (default 443); `http` fetches `scheme://host:port/path` (`scheme` defaults to `http` for port 80 and `https` otherwise) (the wide IP's own name
unless `host` is set) and optionally requires `match` in the body. `timeout_ms` (100 to 5000, default 1000) bounds one check.
PowerDNS runs the checks from its own address every few seconds. A member PowerDNS has not checked yet is assumed up.

## Check it

```sh
syctl api GET /tenants/acme/gslb/status        # in_sync | drift | pending | error | off, per wide IP
dig +short @ns1.example.com shop.gslb.example.com
```

`drift` means PowerDNS holds something other than what the controller would write now: someone edited it, or a VIP moved and the
job has not run. Nothing is changed automatically; `POST /tenants/acme/gslb/sync` rewrites everything. `error` carries the reason
of the last failed push (`PowerDNS is not reachable...`); the job retries.

Answers are cached for the TTL, so a change is seen within one TTL (default 30 s). Lower the TTL before a planned switch.

## DNSSEC

`POST /tenants/{t}/gslb-zones/{zone}/dnssec` signs the zone and returns the DS records; publish them at the parent zone.
`GET .../ds` shows them again. Keys are managed by PowerDNS; rollover is not automated.

IPv6 members are checked from the PowerDNS container, so its Docker network needs IPv6: `deploy/dns/compose.yaml` creates one with a ULA
subnet (`DNS_IPV6_SUBNET`), and the Docker daemon must route it (`"ipv6": true, "ip6tables": true` in `daemon.json`, Docker 27 or later),
or run PowerDNS with host networking.

## Several sites

Run PowerDNS at each site, answer the zone from all of them, and make the others secondaries of the one the controller writes
to (`secondary` zones with `primary` set to it; `allow-axfr-ips`/`also-notify` on the primary). Each site then evaluates the records
and runs the health checks from its own network. [runbooks/gslb-lab.md](runbooks/gslb-lab.md) is the two-host lab check.

## dnsdist (DNS firewall)

`deploy/dns/` puts dnsdist in front of PowerDNS: `ANY`, `AXFR` and `IXFR` are refused, each client network is rate limited
(`DNSDIST_QPS_LIMIT`, default 50 per second, burst 100; over the limit queries are dropped) and abusive networks are blocked for
a minute. Only dnsdist is published on port 53; PowerDNS listens on 5300 inside it. Zone transfers between your own servers use
5300. See [ADR 0164](adr/0164-dnsdist-in-front-of-powerdns.md).

## Safety

A tenant can set only validated fields; none of them reaches the DNS server as code (ADR 0161). Names are lowercase DNS labels, a
zone cannot be created by a tenant, and one tenant's zone cannot shadow another's. Every change is in the audit log
(`gslb_zone.*`, `gslb_wide_ip.*`).

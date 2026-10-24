# DNS records from a virtual service

A virtual service can publish its VIP as DNS records, so creating the service also creates the name. It uses the tenant's
DNS credential — the same credential types used for DNS-01 certificates (see [Certificates from ACME providers](acme.md)).

## Which credentials can publish address records

| Credential type | Publishes a service's `A`/`AAAA` records | Notes |
| --- | --- | --- |
| Cloudflare | Yes | Finds the zone of each name; `proxied: true` puts the name behind Cloudflare's proxy |
| RFC 2136 (BIND, Knot, PowerDNS ...) | Yes | One zone per credential: the name must be inside its `zone` setting. The name is read back before every write |
| PowerDNS API | Yes | One zone per credential, same rule; records are read and replaced per record set |
| Amazon Route 53 | No | Publishes TXT records for DNS-01 only; a `dns` block reports `error` |
| Azure DNS | No | TXT only; a `dns` block reports `error` |
| Google Cloud DNS | No | TXT only; a `dns` block reports `error` |

`GET /api/v1/dns-provider-types` reports `supports_address_records` per type, so a client can tell before it tries.
The three TXT-only types are honest gaps, not a configuration problem (see [ADR 0226](adr/0226-address-records-on-rfc2136-and-powerdns.md)).

`proxied: true` is Cloudflare only. On RFC 2136 and PowerDNS it is refused with an `error` on the record instead of being
silently published without a proxy, because an unproxied record would expose the origin.

## Turn it on

1. Add a DNS credential for the tenant: one whose `zones` include the domain (`example.test`), and for RFC 2136 or
   PowerDNS one whose `zone` setting is the zone it writes to. The connection is tested with a TXT record first.
2. Add a `dns` block to the virtual service:

```yaml
virtual_services:
  - name: web
    protocol: http
    vip: 192.0.2.20
    dns:
      names: [app.example.test, www.example.test]
      ttl: 300          # default 300; 1 is "automatic" at Cloudflare
      proxied: false    # true sends the name through Cloudflare's proxy (not for UDP, Cloudflare only)
      credential: null  # a credential name; empty picks the one whose zones cover the name
```

An IPv4 VIP creates `A` records, an IPv6 VIP creates `AAAA` records. Wildcard names are not published, and a name belongs to
one service.

## What the controller does

The `sync_dns_records` job runs every minute (and `POST /api/v1/tenants/{t}/dns-records/sync` runs it now):

| Situation | Result |
| --- | --- |
| New name | Record created |
| VIP, TTL or `proxied` changed | Record updated in place |
| Name removed, `dns` block removed, service disabled or deleted | Record deleted |
| Name already has a record with the same value | Adopted (and deleted with the service) |
| Name already has a record with another value | Left alone, state `error` |
| Credential type that cannot publish address records (Route 53, Azure DNS, Google Cloud DNS) | State `error` |
| `proxied: true` on a credential without a proxy (RFC 2136, PowerDNS) | State `error` |

The controller only changes records it created (or adopted); a conflict is an error, never a takeover. On RFC 2136 and
PowerDNS the record is identified by its value, and when a name also holds a value someone else put there — a second
address in the same record set — only the controller's own value is replaced or removed. `GET
/api/v1/tenants/{t}/dns-records` lists the records with their state (`published`, `pending` or `error` with the reason).
Records are written outside change sets, so they are not canaried; they are audited as `dns_record.sync`. The job needs
OpenBao, because the credential's secrets are stored there.

See [ADR 0193](adr/0193-dns-records-from-virtual-services.md) and [ADR 0226](adr/0226-address-records-on-rfc2136-and-powerdns.md).

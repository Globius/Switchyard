# DNS records from a virtual service

A virtual service can publish its VIP as DNS records, so creating the service also creates the name. Today this works with a
Cloudflare DNS credential (the same credential type used for DNS-01 certificates, see [Certificates from ACME providers](acme.md)).

## Turn it on

1. Add a Cloudflare DNS credential for the tenant whose `zones` include the domain (`example.test`).
2. Add a `dns` block to the virtual service:

```yaml
virtual_services:
  - name: web
    protocol: http
    vip: 192.0.2.20
    dns:
      names: [app.example.test, www.example.test]
      ttl: 300          # default 300; 1 is "automatic" at Cloudflare
      proxied: false    # true sends the name through Cloudflare's proxy (not for UDP)
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
| Credential type that cannot publish address records | State `error` |

The controller only changes records it created (or adopted). `GET /api/v1/tenants/{t}/dns-records` lists them with their state
(`published`, `pending` or `error` with the reason). Records are written outside change sets, so they are not canaried; they
are audited as `dns_record.sync`. The job needs OpenBao, because the credential's token is stored there.

See [ADR 0193](adr/0193-dns-records-from-virtual-services.md).

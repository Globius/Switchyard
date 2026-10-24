# ADR 0193: DNS address records published from a virtual service

Status: Accepted

## Context

Creating a service meant a second, manual step: someone had to add the `A` record at the DNS provider. The Cloudflare adapter
already existed for ACME DNS-01 (ADR 0079) but nothing created address records. The parity plan and the audit list the
flow as missing.

## Decision

A virtual service may carry a `dns` block: `names` (host names, no wildcards), `ttl` (default 300), `proxied` (Cloudflare's
proxy, not for UDP) and `credential` (the tenant's DNS credential by name; empty uses the credential whose zones cover the
name, the same rule as ACME).

- The controller keeps one `A` (IPv4 VIP) or `AAAA` (IPv6 VIP) record per name, through the `sync_dns_records` job (every
  minute) or `POST /tenants/{t}/dns-records/sync`. `GET /tenants/{t}/dns-records` lists what it manages and each record's
  state.
- **Ownership.** The `dns_record` table (migration 0033) is the controller's list of what it created. It changes and deletes
  only those records. A name that already has a record with another value is reported as `error` and left alone. A record
  with the same value is adopted; deleting the service then deletes that record too.
- Records follow the service: a moved VIP updates the record in place, and a disabled or deleted service, a removed name or a
  removed `dns` block deletes it. This is not drift correction: the data plane never depends on it, and a failed push is
  retried by the next run and shown on the record.
- One name belongs to one service (`dns_name_taken`).
- Only Cloudflare supports address records in this change. Other credential types report `error` ("cannot publish address
  records"); adding a provider means implementing `upsert_address` and `delete_address` (`adapters/dns/base.py`). RFC 2136
  and PowerDNS now do this too: see [ADR 0226](0226-address-records-on-rfc2136-and-powerdns.md).
- Defaults chosen without asking: the job runs only when the secrets backend is OpenBao (the credentials live there), a
  `proxied` record uses Cloudflare's automatic TTL, and the UI form is left to the UI work (API and CLI first).

## Consequences

DNS changes happen outside the change-set pipeline: they are not canaried or rolled back with a revision. They are audited
(`dns_record.sync`) and visible per record.

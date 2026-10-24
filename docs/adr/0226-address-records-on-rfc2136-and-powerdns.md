# ADR 0226: Address records on RFC 2136 and PowerDNS

Status: Accepted
Date: 2026-10-24

## Context

ADR 0193 published a virtual service's VIP as `A`/`AAAA` records, but only through Cloudflare. The parity plan and the
audit (issue #19) call the gap a deficiency: a tenant whose authoritative DNS is BIND, Knot or PowerDNS had to add the
record by hand, and the API said so only by failing with "cannot publish address records". This record supersedes the
"Cloudflare only" bullet of ADR 0193.

The two providers that can be driven without a cloud SDK are RFC 2136 dynamic updates (BIND, Knot, PowerDNS in
`rfc2136` mode) and the PowerDNS HTTP API. Route 53, Azure DNS and Google Cloud DNS would each need a new SDK dependency
for a signature and an API shape we cannot exercise without an account.

## Decision

`Rfc2136` and `PowerDns` (`adapters/dns/`) implement the `AddressRecords` protocol (`upsert_address`,
`delete_address`). `ProviderType.supports_address_records` declares the capability in the catalogue and
`GET /dns-provider-types` reports it, so the API and the UI can say which credential types publish records instead of
letting the operator find out from a failed sync. Route 53, Azure DNS and Google Cloud DNS declare `false` and the sync
reports `error` for them; nothing else about them changes.

- **The handle is the record's value.** Both protocols identify a record inside a name by its RDATA, not by an id as
  Cloudflare does. `upsert_address` returns the address it wrote and `delete_address` accepts it, so the `dns_record`
  table keeps working unchanged.
- **A read before a write.** PowerDNS reads the record set (`GET .../rrsets?rrset_name=&rrset_type=`); RFC 2136 reads it
  with one signed query (`dns.query.tcp`). If the name has a value the controller did not create, the call fails with
  `DnsError` and nothing is written; an identical value is adopted. When the controller does own a value in a set that
  also holds someone else's, only its own value is replaced — the other values are never deleted.
- **No proxy.** `proxied: true` means "traffic through Cloudflare's proxy". Neither protocol can express it, so it is
  refused with `DnsError` rather than silently published unproxied, which would expose the origin.
- **One rule for names and zones.** `base.address_target(name, address, zone)` normalises and checks the name (exact
  name, inside the credential's zone, DNS labels) and the value (IP literal, `A` or `AAAA`). `base.check_zone_name`
  validates a credential's `zone` setting, so a bad zone is rejected when the credential is saved, not on the first sync.

## Consequences

A service whose `dns` block points at an RFC 2136 or PowerDNS credential now creates, updates and removes its records like
a Cloudflare one, with the same ownership rule. RFC 2136 costs one extra query per change. `proxied` stays Cloudflare
only, and the docs say so. Route 53, Azure and Google remain TXT-only until someone adds an adapter and its fake; the
flag (and the test that the flag matches `supports_addresses`) keeps that honest.

## Alternatives considered

- **Ownership without a read.** RFC 2136 could assert "the RRset must not exist" with a prerequisite and create the record
  blind. It still could not tell adoption from conflict, and the sync would report an error for a name that already
  resolves to the same address, so the read stays.
- **A record id in the handle.** Neither protocol has one. A content hash would be opaque but no more precise than the
  value itself, and it would have to be looked up again on every write anyway.
- **Ignoring `proxied` on providers without a proxy.** Fewer errors, but a service that asks to be hidden behind the
  proxy would quietly publish its origin address.

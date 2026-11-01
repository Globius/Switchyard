# 0230. Per-host client-IP restoration

Status: Accepted (stacked on [0229](0229-restore-client-ip-without-allow-list.md); merge after it)
Date: 2026-11-01

## Context

Client-IP restoration ([0229](0229-restore-client-ip-without-allow-list.md)) is a service-wide setting. A shared host/SNI service
serves hosts behind a CDN (the header must be restored) and hosts that clients reach directly (the header must not be believed:
it is spoofable). With a source list this is workable, but a per-host choice is the right primitive, and it removes the need for
the any-source mode on mixed services.

## Decision

1. **A route may carry `client_ip`** (`RouteClientIp`): `enabled` (default true), `header`, `allow_list_id`, `from_any_source`.
   `enabled: false` means "do not restore for this host". The block has the same rules as the service level: a header needs
   exactly one of `allow_list_id` or `from_any_source: true`; the two never combine; `enabled: false` takes nothing else.
2. **Replace, not merge.** A matched block replaces the service's `restore_client_ip_header` / `allow_list_id` /
   `restore_client_ip_from_any_source` as a whole. Absent (`null`) inherits the service. Merging field by field would let a
   route silently combine an inherited list with its own header.
3. **Precedence for a request:** exact-host route > wildcard-host route (`*.` is single-label, so at most one wildcard matches) >
   service level. This is the same exact-over-wildcard rule host routing uses. `enforce_allow_list` and `deny_list_id` stay
   service-wide and run on the restored address.
4. **Only host-only routes** (the host is the only match condition) may set `client_ip`: the client address is settled before
   the path or any other condition is read, so a per-path value would be ambiguous. Path, method, header, query, source or SNI
   conditions on the route are a validation error, as are two routes with `client_ip` for the same host, and non-HTTP services.
   The route's `allow_list_id` must reference an allow-list policy set of the same tenant (checked at snapshot validation).
5. **Rendering.** Only when a service has an override: the host is computed first (`txn.host`), each override sets
   `txn.sy_cip` to its route index (wildcards first, exact hosts after, so exact wins), the service-level rules get an extra
   `!{ var(txn.sy_cip) -m found }` and each override's rules an extra `{ var(txn.sy_cip) -m int N }`. All rules run before
   anything uses `src`. The safety rules of 0229 (single bare address, otherwise deleted; `X-Forwarded-For` dropped and rebuilt)
   are the same code. Services without overrides render byte-identically.
6. **Storage.** `vs_route.client_ip` JSONB, nullable (migration 0052); existing rows inherit.

## Consequences

- A host with `enabled: false` leaves the header untouched (as on a service without restoration), so the backend sees what the
  client sent: do not trust it there.
- A mixed service can use a list per CDN host and `enabled: false` for device hosts, with no any-source mode.
- UI: the service overview shows the override per route. Editing it is by the routes JSON/API/provider; a form control is left.

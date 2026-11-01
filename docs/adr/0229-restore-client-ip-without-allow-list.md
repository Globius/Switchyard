# 0229. Restoring the client address without an allow list

Status: Accepted
Date: 2026-11-01

## Context

`restore_client_ip_header` replaced the connection source with the address in a header such as `CF-Connecting-IP`, but the model
required `allow_list_id` and the renderer emitted the rules only when a list existed (the header was honoured only when `src`
matched the list, [0147](0147-country-and-reputation-ip-lists.md) builds on that order). A service behind a CDN without an
allow list therefore saw the CDN edge address: deny lists, rate limits and logs judged the edge, not the client. Needing the CDN
ranges in order to read a CDN header is the wrong coupling; the allow list answers "who may connect" and "whom do I believe",
and the second can be asked on its own.

## Decision

1. **The allow list becomes an optional trust restriction.** With `allow_list_id` the behaviour is unchanged: the header is
   honoured only from a listed source and removed from any other. `enforce_allow_list` (reject sources outside the list) is a
   separate feature and still needs `allow_list_id`.
2. **No list means an explicit opt-in.** A new boolean `restore_client_ip_from_any_source` (default `false`) states that every
   source is believed. `restore_client_ip_header` without a list and without that flag is refused by the model (422), so the
   unsafe mode is never reached by omission. The flag needs the header, cannot be combined with `allow_list_id` (the list would
   silently win or lose) and is HTTP only, like the header. Migration 0051 adds the column with default `false`; existing rows
   all have a list and render as before.
3. **The header is validated either way.** The rules use the header only when it is exactly one header line, made of address
   characters, that HAProxy parses as one IPv4/IPv6 address (`req.fhdr_cnt`, `req.fhdr -m reg`, `req.hdr_ip`). `set-src` takes
   `req.hdr_ip(H)`. Any other value (list, second line, hostname, garbage) is not used and is deleted, in both modes, and a
   header from an untrusted source is deleted as before. `X-Forwarded-For` is still dropped and rebuilt from the restored source.
   Verified with the HAProxy 3.x package binary (`haproxy -c` over every golden case, and requests with valid v4/v6, comma list,
   and malformed values).

## Trade-off

With `restore_client_ip_from_any_source` a client that reaches the VIP without passing the CDN can claim any address and evade a
deny list or rate limit keyed on `src`. The flag exists to make that visible, its UI text and the docs say so, and the
recommended setup is still the allow list (or a network path where only the CDN can reach the VIP). The rendered rules cannot
tell a CDN from an attacker; the operator makes that call.

The generated rule text for existing list-based services changes (the validity checks and a second `del-header`), which rolls a
new render hash through a canary on upgrade; the behaviour for a valid header from a listed source is identical, and a malformed
value from a listed source is now removed instead of forwarded.

## Consequences

- API/OpenAPI, the UI client, the OpenTofu provider and the UDP forbidden-field lists gain `restore_client_ip_from_any_source`.
- Golden case 36 pins the list, any-source and no-header renderings.

# 0132. Identity headers to the origin

Status: Accepted
Date: 2026-10-12

## Context

Plan item E3. Origins want to know who is calling without their own login. The same hygiene rule as the client-IP header applies:
a client must never be able to supply what the origin trusts.

## Decision

`identity_headers` on the access set names the headers the engine writes for a signed-in user:

| Field | Default | Value |
| --- | --- | --- |
| `user` | `X-Auth-User` | the proxy's `X-Forwarded-User` (the IdP's preferred user name or subject) |
| `email` | `X-Auth-Email` | `X-Forwarded-Email` |
| `groups` | `X-Auth-Groups` | `X-Forwarded-Groups`, comma separated |
| `token` | off | the ID token the IdP signed (`Authorization: Bearer ...` from the proxy, with the prefix removed) |

A name set to `null` turns that header off. The `token` header is the IdP's signed JWT: an origin that wants to verify it fetches
the IdP's keys itself; Switchyard adds no signature of its own. When `token` is on, the proxy passes the bearer token and the
engine moves it into the named header and removes `Authorization`, so an origin's own Authorization handling is not confused.

**Hygiene.** Before anything reads the request, the first hop deletes from the client request: the configured names, the four
proxy headers (`X-Forwarded-User`, `-Email`, `-Groups`, `-Preferred-Username`, `-Access-Token`) and `X-Sy-Access`. The second
hop writes the configured headers from the proxy's values and deletes the proxy headers again. On `public` paths the identity
headers are absent. Names that would collide with message framing, hop-by-hop, `Host`, `Authorization`, the proxy's own headers
or `X-Forwarded-For` and `-Proto` are refused when the set is saved (`cannot carry identity`).

**Trust.** An origin may trust these headers only if its traffic comes only through the engine (network rule, or the origin lock-down
in plan section E). The record does not enforce that.

## Consequences

- Rewrite policies cannot set or remove the configured identity names on requests that need a login in a way that matters: the
  headers are written after request rewrites on the second hop.
- The groups header can be long; the proxy and HAProxy limits apply (`tune.bufsize`).
- Signed assertions for origins that cannot parse an ID token (a Switchyard-signed JWT) are not built.

## Alternatives considered

- **A Switchyard-signed JWT per request.** Needs a key per tenant on every engine and a rotation story; the IdP token covers the first use.

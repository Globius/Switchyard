# 0138. Device posture signals in access rules

Status: Accepted
Date: 2026-10-12

## Context

Plan item E8 (device posture and zero-trust signals). Full ZTNA needs a client or a device agent; what an HTTP load balancer can
use are signals that already travel with the request: the TLS client certificate of a managed device, and claims the identity
provider puts in the ID token (Entra "compliant device", Okta device assurance, an `amr` list that includes `mfa`).

## Decision

An `access` rule (ADR 0131) may carry `posture`, in addition to its mode; it never goes with `public`.

- `device_certificate: true`: the connection presented a client certificate that the port's client CA (ADR 0070) accepted. The service
  needs a port with `client_ca_id` (`posture_needs_client_ca`); use `client_verify: optional` so other paths still work. The first
  hop records it in the header `X-Sy-Device: 1` (removed from client requests, like `X-Sy-Access`), because the loopback hop has no
  TLS. Pairing a certificate with the *user* (this device belongs to this account) is left to a `client_cert` set (ADR 0136), which
  can be attached next to the access set.
- `claims` (up to 8, all must hold): `{name, op, value|values}` with `op` `present`, `true` (JSON `true`), `equals` or `in`, on a top-level
  claim of the ID token (`A-Za-z0-9_-`). The proxy passes the token it verified (`pass_authorization_header`), HAProxy reads it with
  `http_auth_bearer` and `jwt_payload_query`, and removes `Authorization` before the origin (as for the `token` identity header).
- A failing check answers 403, like a failing group rule.
- A JSON boolean claim reaches the comparison as `1` or `0` (found with a real Dex token), not as `true`; `op: true`, and `equals`/`in`
  with `true` or `false`, therefore match both spellings.

**Defaults.** No posture means no posture check. Claims are only as fresh as the session: they are re-read when the proxy refreshes
the session (`cookie_refresh_s`, default 15 minutes), so a device marked non-compliant at the provider is cut off at the next refresh.

**Not built.** Agent-based posture (disk encryption, patch level) and non-HTTP applications: they need a client and a posture
service, a separate product decision. Nested claims and array claims other than `in` on a string value are not supported.

## Consequences

- Posture rides on the same two-pass design as ADR 0130; nothing new runs on the engine.
- `golden/20-access-login` covers the rendering with `haproxy -c`; `test_access.py` checks every operator and the header hygiene.

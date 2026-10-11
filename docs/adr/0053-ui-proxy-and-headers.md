# 0053. UI proxy, security headers and login rate limiting

Status: Accepted
Date: 2026-10-11

## Context
The UI security review (S1) found that nothing in `deploy/` serves or fronts the UI, so no CSP or other headers are
sent, and the controller has no login rate limit.

## Decision
- `deploy/proxy/` builds the UI and serves it from nginx, forwarding `/api/` and `/metrics` to the API (streams
  unbuffered). It is a service in the production Compose on every node; the management SE group's HAProxy
  terminates TLS and balances over the nodes' port 8080.
- Headers: the CSP from the review (self-only, `worker-src 'self' blob:` and inline styles for Monaco,
  `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, and HSTS when
  the request arrived over HTTPS (`X-Forwarded-Proto`).
- Rate limiting: login, OIDC callback and break-glass are limited to 5 requests a minute per client address
  with a burst of 10. The client address is taken from `X-Forwarded-For` only when the sending hop is a private
  address.

## Consequences
The limit is per node, not cluster-wide, so an attacker spreading over three nodes gets three times the
allowance. Behind the SE, the limit works per real client only if HAProxy sends `X-Forwarded-For`; if it does not,
all users share one bucket (the SE's address) and 5 a minute could lock people out: verify this on the first
deploy. The API port stays on loopback by default for recovery access. CSP was tested for headers only, not with
a browser loading Monaco.

## Alternatives considered
Caddy (automatic TLS is not needed behind the SE); rate limiting in the controller (needs shared state across
nodes, so it would go in PostgreSQL; revisit if per-node limits prove too weak).

# 0135. JWT validation for APIs

Status: Accepted
Date: 2026-10-12

## Context

Plan item E7: APIs behind a service are called by programs that carry a bearer token from the tenant's identity provider. There
is no login page; the engine must refuse requests without a valid token, and it must not need the controller or the IdP at
request time.

## Decision

A policy set of `kind: jwt` holds up to 16 ordered `rules`. The first rule whose `match` ([0101](0101-route-match-model.md))
matches decides; a request that matches no rule is not checked. A rule has:

| Field | Meaning |
| --- | --- |
| `issuer` | the `iss` claim must equal it (an `https` URL) |
| `audiences` | up to 8; the `aud` claim (string or array) must contain one; empty means not checked |
| `required_scopes` | up to 8; every one must be in the space-separated `scope` claim |
| `leeway_s` | clock difference tolerated on `exp` (0 to 300, default 30) |
| `keys` | 1 to 8 public keys: `{kid?, alg, public_key_pem}`; `alg` is RS, PS or ES 256, 384 or 512 |
| `subject_header` | optional request header that receives `sub` of a valid token |

**Verification.** HAProxy's `jwt_verify(<alg>,<key file>)` checks the signature and that the token declares that algorithm; the
`http_auth_bearer` fetch supplies the token. The rule sets a per-request flag if any key verifies **and** the claims check out;
without the flag the request is answered `401` with `WWW-Authenticate: Bearer`. A key with a `kid` is tried only for tokens
whose `kid` header equals it; a key without one is tried for every token (so a rotation lists the old and the new key).
Symmetric algorithms (HS256 and others) and `none` do not exist in the model: the engine never holds a shared secret.

**Keys and distribution.** The keys are public, so they live in the policy set (PostgreSQL) and travel to the engines in the
agent bundle as `certs/jwt-<set>-<rule>-<key>.pem` (flat PEM files, the managed `certs` directory, HAProxy reloads when they
change). The controller does not fetch JWKS at run time. `syctl jwks convert <file>` turns a downloaded JWKS into the `keys`
list (RSA and EC; private parameters, `oct` keys and encryption keys are refused). Rotation is an edit of the set through a normal
change set. A job that watches a `jwks_url` and proposes that edit is left for a later block; until then a key change at the IdP
needs a change set before the old key is withdrawn.

**Order.** Token checks are stage 4b of [0100](0100-policy-model-and-request-stages.md): after fixed responses (health paths can
answer without a token) and before rewrites and routing. Like the other early stages they run on the first hop only when an access
set is also attached.

**Subject header hygiene.** The configured header is removed from every request before the rule runs and set only for valid
tokens. The name may not be a framing, hop-by-hop, client-address or proxy header, and may not collide with another identity
header of the service (`identity_header_conflict`). The `Authorization` header is passed on unchanged so the origin can read
claims itself.

**TLS.** A service with a `jwt` set may not carry credentials in clear text: every plain port needs `https_redirect_port`
(`access_needs_tls`).

## Consequences

- No call to the IdP per request and no dependence on the controller: a valid token keeps working through a controller outage.
- Revocation is by expiry only; short token lifetimes are the control. There is no introspection or deny list.
- The claim checks are string matches on the payload. A token whose `scope` is an array (`scp`) is not understood; only the
  space-separated `scope` claim is.
- A key that is not a valid PEM public key is caught by `haproxy -c` before any engine is changed.

## Alternatives considered

- **Fetch JWKS from the IdP on each engine.** Needs egress from every engine and a cache with its own failure modes.
- **Introspection (RFC 7662).** An outbound call per request; not stateless.
- **oauth2-proxy in API mode.** Another process on the hot path for what HAProxy does natively.

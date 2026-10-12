# ADR 0153: JWKS URL on JWT rules

Status: Accepted

## Context

JWT rules carry the issuer's public keys as PEM (ADR 0135). Issuers rotate keys; pasting a JWKS by hand
(`syctl jwks convert`) leaves the rule stale after each rotation.

## Decision

- A rule may name `jwks_url` (an `https` address). `keys` may then be empty.
- The `sync_jwks` job (every 5 minutes) fetches each URL, converts the signing keys with the same code
  as the manual import and, when they differ, opens one change set per tenant as `system:jwks`. It is
  applied like any other (canary, revision, approval where the tenant requires it).
- The fetch is `https` only, does not follow redirects, takes at most 256 KiB, and the host passes the
  address policy used for DNS servers (`SWITCHYARD_JWKS_CIDRS` allows private networks).
- A failed or empty answer, or a document with a private key in it, leaves the keys as they are. A rule
  with no keys answers 401 (closed) until the first fetch has been applied.

## Consequences

Up to five minutes between a rotation and the new key; the old key stays in the rule until the issuer
stops publishing it. The data plane does not fetch anything.

# API tokens and client certificates

**What.** Two policy-set kinds that check machine callers on the engine, without a login page:

- `jwt` checks bearer tokens: signature (RS, PS or ES algorithms), issuer, audience, scopes and expiry, and can pass the
  subject to the origin in a header. Keys are listed in the set, or fetched from a `jwks_url`.
- `client_cert` maps client certificates (by common name or SHA-1 fingerprint) to a user and groups, and decides per path
  whether any valid certificate, a known identity or a group member is needed.

**How to use.** Examples and field lists: [Login in front of a service](../access.md) (the `jwt` and `client_cert` sections).
`syctl jwks convert <file>` turns a downloaded JWKS document into the `keys` list. With `jwks_url` the `sync_jwks` job checks
the URL every five minutes and opens a change set when the keys change.

**Limits.** No shared-secret algorithms (HS256) and no `none`: engines never hold a secret for this. A rule with no keys yet
answers 401. A `client_cert` set needs a TLS port with `client_ca_id`; revocation uses the CA's CRL. A service may combine a
`client_cert` set with an `access` set for two factors.

**Decisions:** [0135](../adr/0135-jwt-validation-for-apis.md), [0136](../adr/0136-client-certificate-identity.md),
[0153](../adr/0153-jwks-url.md).

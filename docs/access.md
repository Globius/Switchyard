# Login in front of a service (access policy sets)

An `access` policy set puts an OpenID Connect login in front of a virtual service. Design: ADRs 0130 to 0133.

## Set it up

1. Register a client for Switchyard at your identity provider. The redirect URL is
   `https://<service host>/oauth2/callback` for each host the service answers on.
2. Store two secrets in OpenBao under your tenant's path: the client secret, and a random cookie key of 16, 24 or 32 bytes
   (for example `openssl rand -hex 16`). The policy set holds only their paths (`client_secret_ref`, `cookie_secret_ref`).
3. Create the policy set and attach it to the service in `policy_set_ids` (one access set per service; every plain port needs
   `https_redirect_port`, because logins must not travel in clear text).

```yaml
kind: access
name: sso
oidc:
  issuer: https://login.example.test/realms/acme
  client_id: switchyard-wiki
  client_secret_ref: kv/tenants/acme/auth/wiki-client
  cookie_secret_ref: kv/tenants/acme/auth/wiki-cookie
default_mode: login          # anything not matched needs a login
rules:
  - match: {path_prefix: /health}
    mode: public
  - match: {path_prefix: /admin}
    mode: groups
    groups: [ops.team]
  - match: {path_prefix: /staff}
    mode: email_domains
    email_domains: [example.test]
identity_headers: {user: X-Auth-User, email: X-Auth-Email, groups: X-Auth-Groups}
```

## What the origin sees

For a signed-in user the origin gets `X-Auth-User`, `X-Auth-Email` and `X-Auth-Groups` (and `X-Auth-Token`, the IdP's signed
ID token, if you name that header). Client copies of those headers are always removed, so the origin can trust them as long as it
accepts traffic only from the engines. Public paths carry no identity.

## Behavior to know

- **Closed by default.** If the engine's authentication proxy is down, protected paths answer 503. Public paths keep working.
- **Sessions** are signed, encrypted cookies (`cookie_expire_s`, default 8 hours). Access to a group is re-checked at each
  refresh (`cookie_refresh_s`, default 15 minutes). To end every session of a set at once, change `cookie_secret_ref` to a new
  secret.
- **Reserved paths:** `auth_path` (default `/oauth2/`) is the login endpoints on every host of the service; sign out with
  `/oauth2/sign_out`.
- **Limits:** rate limits, redirects, fixed responses and rewrites run once, on the first pass; the request is then handed to
  the proxy and comes back on a loopback address of the same engine.
- **Needs on the SE image:** `oauth2-proxy` and a `sy-auth` systemd unit that starts one process per file in
  `/etc/switchyard/auth/` (block P7). Until the image has them, the config is delivered but nothing serves the login.

## Tokens for APIs (`jwt` policy set)

No login page: callers send `Authorization: Bearer <token>` and the engine verifies it (ADR 0135).

```yaml
kind: jwt
name: api-tokens
rules:
  - match: {path_prefix: /v1/admin}
    issuer: https://login.example.test/realms/acme
    audiences: [https://api.example.test/]
    required_scopes: [admin]
    subject_header: X-Jwt-Sub        # receives `sub` of a valid token
    keys:
      - {kid: key-2026, alg: RS256, public_key_pem: "-----BEGIN PUBLIC KEY-----
...
-----END PUBLIC KEY-----
"}
  - match: {path_prefix: /v1}
    issuer: https://login.example.test/realms/acme
    keys: [...]
```

Get the keys from the provider's JWKS: `syctl jwks convert jwks.json` prints the `keys` list. During a key rotation list the old and
the new key; remove the old one in a later change. Requests that match no rule are not checked. A missing, malformed, expired or
wrongly signed token is answered `401`.

## Client certificates as identity (`client_cert` policy set)

For a service whose TLS port has a client CA (ADR 0070), map certificates to users and groups and require them per path
(ADR 0136):

```yaml
kind: client_cert
name: staff-certs
identities:
  - {by: cn, value: alice@example.test, user: alice, groups: [staff, ops]}
  - {by: sha1, value: 0123456789abcdef0123456789abcdef01234567, user: build-robot, groups: [ops]}
rules:
  - {match: {path_prefix: /ops}, mode: groups, groups: [ops]}
  - {match: {path_prefix: /staff}, mode: identity}
  - {mode: valid}          # everything else: any certificate the CA accepts
```

The origin gets `X-Cert-User` and `X-Cert-Groups`. Attach this set and an `access` set to the same service for certificate **and**
login. SAML, LDAP, RADIUS and local-user logins are not built; federate them through an OIDC provider (ADR 0137).

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

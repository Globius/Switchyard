# 0012. User login, sessions and per-tenant roles

Status: Accepted; sessions and role refresh superseded in part by [0030](0030-rbac-matrix-and-access-lifecycle.md)

## Context

ADR 0007 gave the API bearer tokens only. The UI needs people to log in with their identity provider,
to know who they are and what they may do in each tenant. Ticket T1 in
`docs/design/05-frontend-backend-tickets.md` sets the acceptance criteria.

## Decisions

1. **OIDC authorization code with PKCE.** `GET /auth/login/{provider}` redirects to the provider;
   `GET /auth/callback/{provider}` validates the state (single use, ten minutes), exchanges the code and
   validates the ID token (signature through the provider's JWKS, issuer, audience, expiry, nonce).
   Implemented with `httpx` and `joserfc`, not Authlib as the design text suggested: we use a small
   fraction of Authlib and these two libraries are already needed. SAML and provider administration
   screens can follow in U4, as T1 allows.
2. **Our own session.** The callback issues a random cookie value (stored only as SHA-256) with an
   eight-hour absolute lifetime and a one-hour idle timeout. The cookie is `HttpOnly`,
   `SameSite=Strict` and `Secure` behind HTTPS. Writes made with a cookie must send the `X-CSRF-Token`
   header (the value from `GET /me`); bearer tokens are exempt because a browser never sends them
   unasked. An expired session answers `401` as `application/problem+json`.
3. **Roles are decided at login.** Provider groups map to `(tenant, role)` through `idp_role_mapping`
   (`system-admin` has no tenant). A user with no mapped group is refused. The result is the same
   `Principal` that tokens use, now with `tenant_roles`, so a person can be a tenant-admin in one tenant
   and a viewer in another. Routes under `/tenants/{t}` narrow the principal to the role held in that
   tenant before any service checks it, so the service code did not change and `/me` agrees with what
   the routes enforce.
4. **Break-glass.** A local administrator (scrypt password, TOTP secret kept in the secrets backend) can
   log in only when `SWITCHYARD_BREAK_GLASS=1`. Five failures lock the account for 15 minutes. It is
   created with `syctl local-admin`.
5. **Audit.** Login, denied login, failed login, logout, and every break-glass attempt write
   `audit_event` rows. Failures are recorded even though the request is refused.
6. **Configuration.** Providers and mappings are managed with `syctl idp add|map|list|enable`. The
   tables are system-wide and only the admin database role touches them.

## Consequences and limits

- `SameSite=Strict` means the page loaded by the redirect after login may arrive without the cookie
  (browsers treat that navigation as cross-site); the UI's first API call afterwards carries it.
- A session keeps the roles it was created with until it ends; changing a group mapping takes effect at
  the next login. Eight hours is the longest a revoked grant can linger.
- Only one provider protocol (OIDC) works so far; SAML rows are refused with a clear message.

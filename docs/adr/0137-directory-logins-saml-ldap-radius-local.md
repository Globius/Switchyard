# 0137. Directory logins (SAML, LDAP, RADIUS, local users): not built, and why

Status: Accepted
Date: 2026-10-12

## Context

Plan item E5 asks for a SAML service provider and for LDAP, RADIUS and local-user providers, so that a tenant without an OIDC
identity provider can still put a login in front of a service. [0130](0130-auth-service-architecture.md) chose oauth2-proxy,
which speaks OIDC only, and noted that SAML or a better fail mode might push the work into a service of our own.

## Decision

This block does **not** build E5. It records the analysis so the next step starts from facts.

1. **Federate through an OIDC provider first.** Keycloak, Entra ID, Okta, Authentik and Dex all present LDAP, SAML and local
   accounts as OIDC. A tenant points `oidc.issuer` at one of them (possibly one the tenant runs). This covers the use cases at no
   cost to Switchyard and is the supported answer until a tenant cannot run one.
2. **Dex on the engine is not a fit.** Dex would add LDAP and SAML connectors behind the same oauth2-proxy, but it keeps
   authorization codes and sessions in its own storage. Engines in a group are independent (VRRP failover, BGP/ECMP) and a login
   spans several requests, which may land on different engines; the code issued by one engine's Dex would be unknown to the other.
   Dex's shared storages (PostgreSQL, etcd, Kubernetes) would give the engines a database dependency that CLAUDE.md rules out for the
   data plane ("SEs keep serving without the controller").
3. **The shape of a real solution** is a small stateless service of our own (the "Go service in `agent/`" alternative of 0130):
   it terminates the directory protocol, then issues the same signed, encrypted cookie oauth2-proxy would, with the signing key from
   `cookie_secret_ref`, so any engine can validate it. It would replace oauth2-proxy per access set, keep the rule model
   ([0131](0131-access-rules-and-authorization.md)) and identity headers ([0132](0132-identity-headers.md)) unchanged, and be
   selected by a `provider` field next to `oidc` in the access set (`ldap`, `saml`, `local`; the stored settings would hold
   references to bind passwords and password hashes in OpenBao). RADIUS is the same service with a different backend and needs a
   challenge screen for OTP.
4. **When to build it:** when a tenant cannot federate, with that tenant's protocol as the first one. Until then the model has no
   `provider` field and no half-implemented kinds.

## Consequences

- E5 stays open in the plan; the access set can gain `provider` without breaking stored sets (`oidc` becomes the default).
- Tenants with only LDAP or SAML today run a federating OIDC provider or wait.
- Nothing in 0130 to 0136 needs to change for it.

## Alternatives considered

- **Dex with in-memory storage.** Works for one engine per group and breaks silently with two; rejected as a trap.
- **Per-engine session affinity for the login flow** (source hashing at the router). Fragile across failover, and it would pin
  state-less engines to a client.

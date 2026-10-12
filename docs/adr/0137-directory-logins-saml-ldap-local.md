# 0137. Directory logins (SAML, LDAP, local users) through a central login broker

Status: Accepted
Date: 2026-10-12

## Context

Plan item E5 asks for a SAML service provider and LDAP, RADIUS and local-user logins, so that a tenant without an OIDC identity
provider can still put a login in front of a service. [0130](0130-auth-service-architecture.md) chose oauth2-proxy on the engines,
which speaks OIDC only. An earlier draft of this record proposed deferring E5, because a broker on each engine (Dex) keeps its
authorization codes and refresh tokens in the engine's own storage, and engines in a group are independent: a login spans several
requests that can land on different engines.

## Decision

**Run the broker centrally, not on the engines.** Dex runs next to the controller (one place, with persistent storage) and presents
a tenant's directory as an OIDC issuer. The engines' oauth2-proxy talks to that issuer exactly as to any external OIDC provider, so
the engines stay stateless, the rule model ([0131](0131-access-rules-and-authorization.md)) and identity headers
([0132](0132-identity-headers.md)) do not change, and a login completed through one engine is valid on any other (the session is
the oauth2-proxy cookie).

**Objects.** A new policy set `kind: directory` (never attached to a service) holds one *connector*:

| `connector.type` | Settings | Notes |
| --- | --- | --- |
| `ldap` | `host`, `port` (636), `security` (`ldaps` or `starttls`), `root_ca_pem`, `bind_dn`, `bind_password_ref`, user search (`user_base_dn`, `user_filter`, `username_attr`, `id_attr`, `email_attr`, `name_attr`), optional group search (`group_base_dn`, `group_filter`, `group_user_attr`, `group_member_attr`, `group_name_attr`) | no clear-text LDAP; the bind password is an OpenBao reference; DNs, filters and attribute names are validated and the config is dumped as data, never assembled from strings |
| `saml` | `sso_url` (https), `ca_pem` (the provider's signing certificate), `username_attr`, `email_attr`, `groups_attr` | assertions must be signed by `ca_pem` |
| `local` | `users[]`: `username`, `email`, `password_hash_ref` (a bcrypt hash in OpenBao) | passwords are checked by the broker; **local users have no groups**, so an access rule with mode `groups` on a local directory is refused (`directory_has_no_groups`) |

An access set uses a directory with `oidc.directory_id` **instead of** `oidc.issuer`, plus `oidc.redirect_hosts` (the host names
users reach the service on; the broker only redirects back to `https://<host><auth_path>/callback` for these). `client_id`,
`client_secret_ref` and the cookie settings keep their meaning; the broker is the issuer
`<broker URL>/<tenant>/<directory>`. The directory must belong to the same tenant (`wrong_tenant`); its secrets obey the tenant
namespace rule (ADR 0071).

**One Dex instance per directory.** Dex lets any client of an instance use any connector of it, so sharing an instance would let a
tenant's client pick another tenant's directory. Each instance has exactly one connector (or the local password database) and only
the clients of the access sets that name its directory (`staticClients`, redirect URIs from `redirect_hosts`). Instances get loopback
ports from 5600 in tenant/directory order; storage is SQLite on the broker's volume; ID tokens live 10 minutes and refresh tokens as
long as the cookie lifetime of the longest client. The Dex configs contain secrets and are written owner-only.

**Delivery.** `syctl broker render --out DIR` (needs `SWITCHYARD_AUTH_BROKER_URL`, the public base URL such as
`https://auth.example.test/dex`, and the OpenBao settings) reads every tenant's state, resolves the secrets and writes
`dex/<tenant>-<directory>.yaml`, `dex/instances.json` (name, path, port) and `dex/routes.conf` (an nginx snippet for the controller's
public proxy), changing only files whose content changed and removing those of directories no longer used. `deploy/prod/compose.broker.yaml` adds
two services: `broker-render` (loops the command) and `broker` (the Dex image with `deploy/prod/broker/run.sh`, which starts a
Dex per config, restarts one when its file changes and stops it when the file goes). The engines' bundles carry the broker's URL in
`oidc_issuer_url` only; the controller refuses to build a bundle for such an access set while `SWITCHYARD_AUTH_BROKER_URL` is unset.

**RADIUS is not offered.** Dex has no RADIUS connector. RADIUS-only tenants federate through an OIDC provider that has one
(Keycloak, Authentik, FreeRADIUS behind an adapter) or wait for a Dex connector or a service of our own; the model leaves room for a
`radius` connector type.

## Consequences

- The broker is a **control-plane dependency of logins** (new logins and refreshes need it; existing sessions keep working until
  their cookie refresh). It does not affect services without a directory login or traffic that is already authenticated. Run it
  on the controller nodes behind the same proxy; losing it closes protected paths at the next refresh, never opens them.
- The broker's public URL must be reachable by users' browsers and by every engine (oauth2-proxy reads discovery and keys from it).
- A user's groups come from the directory at sign-in and at each token refresh; LDAP group changes apply at the next refresh
  (`cookie_refresh_s`).
- Changing a directory or an access set changes the Dex config; Dex restarts and in-flight logins on that instance fail once.
- An LDAP server that is unreachable from the broker shows the user an error at sign-in; HAProxy and the engines are not involved.
- Real Dex is exercised in `tests/integration/test_broker_dex.py`: every connector type starts from its rendered config, and a local
  user completes the authorization-code flow. The LDAP and SAML connectors are checked up to configuration and discovery, not
  against a live directory.

## Alternatives considered

- **Dex on each engine.** Rejected: engine-local state (see Context).
- **Dex on each engine with shared PostgreSQL.** Makes the data plane depend on a database; CLAUDE.md rules it out.
- **A stateless login service of our own on the engines.** The better long-term fit for SAML and RADIUS and still possible; it would
  replace oauth2-proxy per access set without changing the model. Not needed while the central broker covers the cases.
- **Federate only (no broker).** Supported and still the recommendation when a tenant already has an OIDC provider.

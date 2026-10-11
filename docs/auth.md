# Signing in, roles and access

How people and machines get access to Switchyard, and how the API tells a client what they may do.
The decisions are in [ADR 0012](adr/0012-user-login-and-sessions.md),
[ADR 0030](adr/0030-rbac-matrix-and-access-lifecycle.md) and
[ADR 0031](adr/0031-access-refresh-users-and-codes.md) and
[ADR 0032](adr/0032-idp-admin-test-signin-break-glass.md); the full specification is "Auth and RBAC" in
[02-backend-design.md](design/02-backend-design.md).

## Two ways in

| | Browser session | API token |
| --- | --- | --- |
| For | People using the UI | CI, scripts, `syctl api` |
| Credential | `sy_session` cookie (`HttpOnly`, `SameSite=Strict`, `Secure` behind HTTPS) | `Authorization: Bearer sy_...` |
| Writes | need `X-CSRF-Token` (the value in `GET /me`) | no CSRF header |
| Lifetime | 4 hours absolute, 30 minutes idle (break-glass: 1 hour, 15 minutes) | 90 days by default |
| Roles | recomputed from the provider's groups (below) | one role in one tenant, fixed at creation |

A token cannot create tokens. Tenant tokens are created under `/tenants/{t}/tokens` (`viewer`,
`operator` or `tenant-admin`, never above the creator's role). System tokens (`system-admin`) are a
separate resource.

## Roles

`viewer < operator < tenant-admin < system-admin`. They nest: each role can do everything the weaker
ones can. A person has a role **per tenant**; one `system-admin` grant makes them system administrator
everywhere. Several groups combine as a union, which within a tenant is the highest role; `GET /me`
says which groups produced each grant (`tenants[].via`). `principal` is the author string the API
stores for your writes, such as a draft's `author`: `user:<provider>:<subject>` (for example
`user:local:breakglass`). Compare it to `author` to tell which drafts are yours.

## What a role may do: `GET /permissions`

```json
{
  "roles": ["viewer", "operator", "tenant-admin", "system-admin"],
  "permissions": [
    {"verb": "apply", "object": "change_set", "min_role": "operator", "description": "..."}
  ]
}
```

`can(verb, object)` for a tenant is `role_in(tenant) >= min_role`. The `verb` and `object` strings are
published as enums in the OpenAPI document (`PermissionVerb`, `PermissionObject`). The matrix:

| verb | object | min role |
| --- | --- | --- |
| `read` | `tenant_data` | viewer |
| `write` | `application` (virtual service, pool, monitor, policy set, certificate record) | operator |
| `edit` | `draft` | operator |
| `request_approval` | `change_set` | operator |
| `apply` | `change_set` | operator |
| `set_state` | `member` | operator |
| `manage` | `certificate` | operator |
| `retry`, `cancel` | `job_own` (jobs of an operator's own actions: apply, certificates) | operator |
| `write` | `infrastructure` (network, VIP pool, SE group, routing profile) | tenant-admin |
| `approve`, `reject` | `change_set` (never the author or the requester) | tenant-admin |
| `rollback` | `revision` | tenant-admin |
| `retry`, `cancel` | `job` (any job) | tenant-admin |
| `manage` | `engine`; `create` `enrollment_token`; `deploy` `se_group`; `apply` `sdn` | tenant-admin |
| `enable` | `require_approval` | tenant-admin |
| `manage` | `token`; `manage` `role_mapping` (viewer and operator, own tenant) | tenant-admin |
| `list` | `tenant_user` | tenant-admin |
| `apply_bypass` | `change_set` (needs a recorded reason) | system-admin |
| `disable` `require_approval`; `change` `quota` | | system-admin |
| `manage` | `role_mapping_privileged`, `tenant`, `identity_provider`, `user`, `system_token`, `break_glass` | system-admin |

The matrix is `controller/switchyard/domain/permissions.py`; tests compare it with the table in
02 and with the real status code of every route.

## Errors a client can switch on

401 and 403 answers are `application/problem+json` with a stable `code`:

| status | `code` | meaning | what the UI does |
| --- | --- | --- | --- |
| 401 | `unauthenticated` | no credentials | go to the login page |
| 401 | `invalid_token` | bad, expired or revoked bearer token | fix the token |
| 401 | `session_expired` | the session ended (lifetime, idle, revoked, or its last grant was removed) | sign in again |
| 403 | `csrf` | missing or wrong `X-CSRF-Token` | reload `GET /me` |
| 403 | `role_required` | the role is too weak; `required_role` names the weakest that may | disable the control |
| 403 | `user_disabled` | an administrator disabled this person | show the "disabled" page |
| 403 | `forbidden` | anything else (another tenant, a token creating a token, approval rules) | show `detail` |

## When a role change applies

- Editing a role mapping changes people's roles **within a minute**, without signing anyone out: the
  session keeps the groups the provider returned at sign-in and recomputes the grants from them against
  the current mappings when they are 60 seconds old.
- `POST /auth/refresh` (needs the CSRF header) does it at once and returns the `Me` payload. It does
  not extend the session.
- A person who loses their last grant is signed out (the next call answers `401 session_expired`).
- A change of **group membership at the provider** is seen only at the next sign-in, at most four hours
  away. To cut access sooner, `POST /users/{id}/revoke-sessions` or `POST /users/{id}/disable`.

## When sign-in fails

The browser is redirected to `/login?error=<code>`:

| code | meaning |
| --- | --- |
| `provider_error` | the provider refused, sent a bad response, or the request was malformed |
| `expired` | the sign-in attempt was too old or already used; start again |
| `no_access` | none of the person's groups is mapped to a role |
| `disabled` | an administrator disabled the account |

## Administration endpoints

All audited. `If-Match` carries the `version` on `PATCH`.

| Endpoint | Who |
| --- | --- |
| `GET/POST /role-mappings`, `GET/PATCH/DELETE /role-mappings/{id}` | system-admin |
| `GET/POST /tenants/{t}/role-mappings`, `PATCH/DELETE /tenants/{t}/role-mappings/{id}` | tenant-admin (viewer and operator only, own tenant) |
| `GET /users`, `GET /users/{id}`, `POST /users/{id}/disable`, `/enable`, `/revoke-sessions`, `DELETE /users/{id}` | system-admin |
| `GET /tenants/{t}/users` | tenant-admin (people with a grant in the tenant, that tenant's grants only) |

A mapping is `{provider, group_name, label, tenant_id, role}`; `system-admin` takes no tenant. Group
names match exactly and case-sensitively. `(provider, group, tenant)` is unique (`409` otherwise).
`label` is free text for providers that send opaque ids (Entra ID).

Users are created at sign-in (`user_account`: name, e-mail, the groups of the last sign-in, first and
last sign-in; no secrets). The break-glass administrator has no row. `tenant.oidc_group` no longer
exists: use role mappings (the old values were copied to the audit log as
`tenant.oidc_group_dropped`).

## Identity providers (system-admin)

`GET/POST /identity-providers`, `GET/PATCH/DELETE /identity-providers/{id}` (`If-Match`). The body is
discriminated by `protocol`.

- **OIDC:** `issuer_url`, `client_id`, `client_secret` (write-only; stored in the secrets backend) or
  `client_secret_ref`, `scopes`, `username_claim`, `email_claim`, `groups_claim`. A discovery document
  that cannot be fetched is a warning on the response, not a refusal.
- **SAML:** `idp_metadata_url` (https) or `idp_metadata_xml` fill `idp_entity_id`, `sso_url` and
  `signing_certificates`; or send them directly. `sign_requests: true` makes the controller generate a
  signing key (kept in the secrets backend) and publish its certificate in the SP metadata
  (`GET /auth/saml/{id}/metadata`). The read shows `acs_url` and `sp_metadata_url` to give the IdP.
- **Test sign-in:** `POST /identity-providers/{id}/test` returns `{test_id, authorize_url}`; open the
  URL in the browser; poll `GET .../test/{test_id}` (`pending`, then the claims, `groups`,
  `would_grant` and `warnings` once, then 404; five minutes at most). It creates no session and no
  user.

## Break-glass (system-admin)

`GET/PUT /system/break-glass`, `POST /system/break-glass/admin` (generated password and TOTP secret
shown once, with an `otpauth_uri` for a QR code), `POST .../admin/confirm` (`{totp}`),
`DELETE .../admin`. The administrator cannot sign in until confirmed. Enabling needs a reason and
lasts 1 to 1440 minutes (default 240). `forced_by_deployment: true` (`SWITCHYARD_BREAK_GLASS=1`) is
read-only. `GET /auth/providers` says `break_glass: true` only while it is on.

## System tokens (system-admin)

`GET/POST /system/tokens`, `DELETE /system/tokens/{id}`: role `system-admin`, 30 days by default, 90
at most, shown once. Tenant routes cannot create them. A token cannot create tokens, so a signed-in
person does it.

## Audit actions written by this area

`auth.login`, `auth.login_failed`, `auth.login_denied`, `auth.logout`, `auth.session_revoked`,
`auth.break_glass_login`, `auth.break_glass_failed`, `role_mapping.create`, `role_mapping.update`,
`role_mapping.delete`, `user.disable`, `user.enable`, `user.revoke_sessions`, `user.delete`,
`tenant.oidc_group_dropped`, `idp.created`, `idp.updated`, `idp.deleted`, `idp.test_started`,
`idp.test_done`, `break_glass.admin_created`, `break_glass.admin_confirmed`,
`break_glass.confirm_failed`, `break_glass.enabled`, `break_glass.disabled`,
`break_glass.admin_deleted`, `token.system_used`.

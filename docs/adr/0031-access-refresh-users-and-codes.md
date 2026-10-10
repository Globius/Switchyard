# 0031. How access refresh, users and error codes are implemented

Status: Accepted
Date: 2026-10-10

## Context

ADR 0030 decided the permission matrix, grant recomputation, the users table and the removal of
`tenant.oidc_group`. Building it left a few choices the ADR does not spell out.

## Decisions

1. **The matrix is data keyed by `(verb, object)`, and a row number ties it to the document.**
   `domain/permissions.py` holds one `Permission` per cell with the number of its row in the table
   in 02. A unit test parses that table and compares the weakest role of every row, so the document,
   the code and `GET /permissions` cannot disagree. A second test calls every route with a token of
   each role and checks the status and the `required_role` in the 403; a route missing from the test's
   table fails the build, so a new endpoint cannot ship without a permission decision.
2. **Verbs and objects are published as OpenAPI enums** (`PermissionVerb`, `PermissionObject`), so the
   frontend can type `can()` and a typo is a compile error. A test fails when the enums and the matrix
   differ.
3. **Job retry and cancel check the job's kind.** Kinds an operator starts (`apply_change_set`,
   `issue_cert`, `renew_certs`) need `job_own` (operator); every other kind needs `job` (tenant-admin).
   An operator cannot cancel a deployment, SDN or retirement job.
4. **A token cannot create tokens.** Detected from the principal's actor (`token:` prefix); the answer
   is `403 forbidden`, whatever the token's role.
5. **Sessions store the provider's groups** (`user_session.groups`, at most 200) and
   `grants_computed_at`. `authenticate` recomputes when the last computation is more than 60 seconds
   old; `POST /auth/refresh` forces it. A session with no grant left is revoked and the call answers
   `401 session_expired`. The absolute expiry is never moved. Break-glass sessions have fixed grants and
   shorter limits (1 hour, 15 minutes idle).
6. **A disabled user gets a distinct answer.** Disabling revokes the sessions, but a call with such a
   cookie answers `403 user_disabled` (not `401`), so the UI can explain instead of showing a login
   prompt. Sign-in is refused with `/login?error=disabled`.
7. **`user_account` is upserted by `(provider_id, subject)` in the sign-in transaction** with an
   `INSERT ... ON CONFLICT DO UPDATE`, so two simultaneous sign-ins make one row. `last_seen_at` moves
   at most once a minute. Deleting a user cascades to its sessions; the break-glass administrator has no
   row.
8. **`/users` lists with `last_login_at desc, id desc` and a keyset cursor** (ADR 0014); `q` is a
   case-insensitive substring of name and e-mail with `%` and `_` escaped. The `tenant` filter keeps
   people whose stored groups match a mapping of that tenant. System-admin mappings are not "a grant in
   the tenant" for `GET /tenants/{t}/users`.
9. **Role mappings authorize before they look up.** A caller too weak for the route gets the same 403
   whether or not the id exists. A tenant administrator editing a `tenant-admin` mapping, or promoting
   a mapping to it, is refused with `required_role: system-admin`.
10. **Problem `code` values** are `unauthenticated`, `invalid_token`, `session_expired`, `csrf`,
    `forbidden`, `role_required` (with `required_role`) and `user_disabled`, declared as an enum in the
    OpenAPI `ProblemOut`.
11. **Failed browser logins redirect** to `/login?error=<code>` for both OIDC and SAML callbacks,
    including a provider-reported `error`. Starting a login with an unknown provider still answers
    `400`, since that is not a provider round trip.
12. **Migration 0012** copies each non-empty `tenant.oidc_group` to the audit log
    (`tenant.oidc_group_dropped`, actor `system:migration`) and drops the column. Its downgrade adds the
    column back empty. The OpenAPI breaking-change check is expected to flag the removed field in this
    merge request only.

## Consequences

- Every service call that used a role literal now reads the matrix; changing who may do something is
  one edit in `permissions.py` plus the table in 02, and the tests say if one was forgotten.
- A grants query runs at most once a minute per session, on the admin database role.
- Group-membership changes at the provider are still seen only at the next sign-in; the short session
  lifetime and `revoke-sessions` are the answer (ADR 0030).

## Alternatives considered

- **Declare the permission on each route** (a decorator): the check would live next to the route, but
  the services are called from jobs and the CLI too, so the check stays in the service layer and the
  test table lists the routes instead.
- **Recompute on every request:** simplest, but one extra query per call for little gain; 60 seconds
  matches the promise made to administrators.

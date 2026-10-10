# 0030. Permission matrix, multi-group grants, role refresh, users and token scope

Status: Accepted
Date: 2026-10-10

## Context

ADR 0012 shipped OIDC login, sessions, per-tenant roles and the break-glass administrator. The
frontend design (Claude Design mockups) then asked seven questions that the design docs left open
and that every disabled control, admin screen and the SAML work depend on. The roles were described
loosely ("operator: apps, certs, drain, apply"), `tenant.oidc_group` duplicates the role mappings,
a role change only took effect at the next login, there was no list of people who have signed in,
and tokens were described as per tenant while a `system-admin` token had no route that could make it.
This ADR fixes the answers. The full tables and endpoint shapes are in `docs/design/02-backend-design.md`
("Auth and RBAC") and the tickets in `docs/design/05-frontend-backend-tickets.md` (G1).

## Decisions

1. **One permission matrix, in code.** Roles stay `viewer < operator < tenant-admin < system-admin`
   and nest (each role can do everything the weaker ones can). The matrix in 02 is the specification;
   `domain/permissions.py` holds the same data, services call it instead of ad hoc
   `require_role(...)` literals, a test fails if the code and the table in 02 disagree, and
   `GET /permissions` serves it so the UI's `can()` hook reads the backend's answer instead of
   copying it. What the code already enforced is kept, with three changes: audit events stay
   readable by viewers, retrying or cancelling a job needs the role of the action that created it
   (so an operator cannot cancel a deployment), and API tokens cannot create API tokens.
2. **Approval.** A change set is approved only by a `tenant-admin` (or `system-admin`) who is neither
   its author nor the person who requested approval. Operators request approval but never approve.
   There is no separate `approver` permission; the design text that mentioned one is replaced.
3. **Operators.** An operator may create, update and delete virtual services, pools and members,
   health monitors, policy sets and certificate records, and may apply, drain and manage jobs of
   those. An operator may not change networks, VIP pools, SE groups or routing profiles, may not
   register, enable, disable, retire or deploy engines, and may not manage tokens or mappings.
4. **Several groups: the grants combine as a union, which is the highest role.** Because roles nest,
   the union of grants in one tenant equals the highest role, so "highest wins" and "grants combine"
   are the same rule and the UI statement is correct. Across tenants the grants stay separate.
   One system-admin mapping makes a person system-admin everywhere. `/me` also returns which groups
   produced each grant (`via`) so the UI can explain it.
5. **Role changes: next refresh, not next login, and never silent.** A session stores the groups its
   provider returned at login. Grants are recomputed from those groups and the *current* mappings
   when the session is more than 60 seconds past its last computation, and immediately by
   `POST /auth/refresh`. So editing a mapping takes effect within a minute without ending sessions.
   A person who loses every grant has their session revoked. Changes to group *membership* at the
   provider are seen only at the next sign-in; sessions therefore get shorter lives (four hours
   absolute, 30 minutes idle; break-glass one hour and 15 minutes), and administrators can end
   sessions at once with `POST /users/{id}/revoke-sessions` or by disabling the user.
6. **`tenant.oidc_group` is removed.** Login never read it; it duplicates the mappings and cannot say
   which provider or role it meant. A migration copies each non-empty value into the audit log
   (`tenant.oidc_group_dropped`) and drops the column; the API stops accepting and returning it.
   This is a deliberate OpenAPI breaking change, allowed because the only client is the UI under
   construction. Role mappings are the only way a group grants access.
7. **Users table.** `user_account` records every person seen at sign-in: provider, subject,
   username, email, the groups from the last sign-in, first and last sign-in, last seen, and
   `disabled_at`. It holds no secrets, passwords or tokens. It answers "who has access" and "what can
   this person do" (grants computed from stored groups and current mappings), and lets a
   system-admin disable or delete a person.
8. **Token scope.** Tokens are per tenant (`/tenants/{t}/tokens`), roles `viewer`, `operator` or
   `tenant-admin`, never above the creator's role. System tokens are a separate resource
   (`/system/tokens`, system-admin only, role `system-admin`, 30-day default lifetime, 90-day maximum).
   No route accepted a system token before, so this documents what was possible only from code.
9. **Break-glass.** Still the same login (password and TOTP, lockout, audited). A system-admin can now
   enroll the administrator and its TOTP secret and switch it on for a limited time through the API.
   `SWITCHYARD_BREAK_GLASS=1` stays and forces it on, because the case it exists for (every provider is
   down, nobody can sign in to switch it on) cannot depend on a session.
10. **SAML (for the implementer of that ticket).** SP-initiated web SSO only; the assertion consumer
    service is `POST /auth/callback/{provider}` next to the OIDC `GET` of the same path; signed
    assertions or responses are required; the request id is checked against the stored login state
    and assertion ids are remembered until they expire; identity provider settings and the metadata
    download are in 02. Failed browser logins redirect to `/login?error=<code>` instead of showing JSON.

## Consequences

- ADR 0012's statements "a session keeps the roles it was created with" and "mappings take effect at
  the next login" are superseded by decision 5; its eight-hour/one-hour lifetimes by decision 5.
- The matrix becomes testable: one parametrized test walks every route with every role and compares
  the status code with the matrix.
- Operators can still delete a pool in use only if the validator allows it; the matrix says who may
  try, not whether the change set is valid.
- Removing `oidc_group` and adding `required_role` to 403 problems change the OpenAPI document; the
  breaking-change check needs an explicit allowance in the same merge request.
- Group names are compared exactly and case-sensitively. Providers that send object ids (Entra ID
  groups) need the id in the mapping; the mapping has a free-text `label` so the screen can show a name.

## Alternatives considered

- **A separate `approver` permission** (as 02 first said): more flexible, but a fifth role-like
  concept for the UI to explain, and operators approving their own team's work weakens four-eyes.
  Rejected; revisit if a tenant needs a delegated approver.
- **Highest role only, ignoring other grants:** identical result because roles nest, so no reason to
  special-case. If roles ever stop nesting (custom roles), the union rule is the one that survives.
- **End all sessions when a mapping changes:** simple and strict, but it logs out everyone on any
  edit. Rejected in favour of recomputing grants.
- **Keep `oidc_group` as "any member is a viewer":** a second, hidden path to access that the
  mappings screen does not show. Rejected.
- **Store provider refresh tokens to re-read groups:** would put long-lived provider credentials in
  PostgreSQL, against the secrets rule. Rejected; short sessions and explicit revocation instead.

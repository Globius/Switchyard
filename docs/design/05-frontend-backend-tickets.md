# Backend tickets required by the frontend

Date: 2026-10-10. Derived from the gaps in [04-frontend-scope.md](04-frontend-scope.md). One section per ticket, written to paste into GitLab issues. Gap ids (G1…) match that doc.

Rules for all tickets (CLAUDE.md): tests, docstrings, user-visible behavior in `docs/`, an ADR for decisions, regenerate and commit `docs/api/openapi.json` (CI runs a breaking-change check).

---

## T1. User login and sessions (G1) — blocks U1 login, `can()` hook

**Problem.** Only `Authorization: Bearer <api token>` exists. The UI needs OIDC/SAML login, a session cookie and a way to know who the user is and what roles they hold.

**Scope**
- Session: HttpOnly, SameSite=Strict cookie issued after login; CSRF header required on writes; the same `Principal` model as tokens (ADR 0007).
- `GET /api/v1/auth/providers` → `[{id, name, protocol: oidc|saml}]` (public, no auth).
- `GET /api/v1/auth/login/{provider}` (redirect) and callback; `POST /api/v1/auth/logout`.
- `GET /api/v1/me` → `{id, name, email, is_system, tenants: [{id, name, role}]}`.
- Break-glass local admin (password + TOTP), off by default.
- Bearer tokens keep working for CI.

**Acceptance.** Login round-trips against a fake OIDC provider in tests; expired session returns 401 problem+json; `/me` roles match what route checks enforce; audit events for login, logout, break-glass use.

**Can slip to** U4 for SAML and identity-provider admin CRUD; OIDC + `/me` must land before U1 ships.

**Status.** Built (ADR 0012) except SAML, identity-provider and mapping APIs, users and the refresh rules; those are specified in **G1** below.

---

## T2. Typed response models (G2) — blocks the generated client

**Problem.** Requests have models; responses are `object` / `Page{items: object[]}`, so the TS client types reads as `unknown`.

**Scope**
- Per object type a `*Read` model = create fields + `id`, `version`, `tenant_id`, `draft_status` (enum), `changed_fields`. Typed `Page[T]` per list.
- Typed responses for: change sets (list item and detail incl. `ops`), diff, revisions (list, detail), jobs, status (`EngineStatus` with typed `members`, `agent`, `drift`), audit events, tokens, engines, tenants, `Accepted`, member-state result.
- Document `ETag` on object reads and the 412/428 problem responses.
- Type `warnings[]` on the diff (see T6).
- Add summaries/descriptions and at least one example per route (MSW mocks are built from examples).
- List additions: `created_at`, `base_revision`, op count on `GET /change-sets`; cursor pagination on revisions and change sets.

**Acceptance.** `openapi-typescript` output has no `unknown` for any 2xx body; a contract test fails if a route returns a body not matching its declared model.

---

## T3. Rendered config per object (G3)

**Problem.** The only rendered output is the change-set diff, with an undocumented `files` shape. Every object page needs a Rendered config tab.

**Scope**
- `GET /tenants/{t}/{virtual-services|pools|…}/{id}/rendered?change_set=` → `{se_group_id, file, language, content, fingerprint, range?}` for the fragment(s) the object produced (live, or draft with `change_set`).
- Document and type `diff.se_groups[].files[]` (`path`, `before`, `after` or a unified diff) so the Monaco diff view has both sides.
- Reuse the pure renderer; no new I/O. Same determinism guarantees (ADR 0004).

**Acceptance.** Golden test per object type; draft and live outputs differ exactly where the diff says.

---

## T4. Schemas for policy sets and routing profiles (G7)

**Problem.** Their bodies are free-form dicts, so forms can't be generated.

**Scope**
- Publish `oneOf` with discriminator `kind`: policy sets `redirect | allow_list`; routing profiles `vrrp | bgp` (models already exist in `domain/models.py`). Create, patch and read variants.
- Field-level validation errors keep `errors[].path`.

**Acceptance.** OpenAPI contains the union with discriminator mapping; generated TS narrows on `kind`; existing tests unchanged.

---

## T5. Job model, list and stages (G6, part of G5)

**Problem.** `GET /jobs/{id}` returns `{kind, state, attempts, error, result}` only. The UI needs a Jobs page and apply progress.

**Scope**
- Typed `Job`: `id`, `kind`, `state`, `tenant_id`, `change_set_id?`, `created_at`, `started_at`, `finished_at`, `attempts`, `error`, `result` (typed per kind), `stages[]` (`name`, `state`, `started_at`, `finished_at`, `error`).
- Apply stages: lock, render, validate, canary, rollout, commit. Distinguish *reverted/rejected* (finished, `ok: false`) from *retrying* (ADR 0007).
- `GET /tenants/{t}/jobs?state&kind&cursor&limit`, `GET …/jobs/{id}/log`, `POST …/jobs/{id}/retry`, `POST …/jobs/{id}/cancel` (role-checked, audited). Keep `/jobs/{id}` working.
- Progress delivery: add `job_progress` to the SSE stream (or a per-job stream); polling is acceptable for the first cut.

**Acceptance.** Applying a change set exposes each stage with timing; a failed canary shows the failing stage and error; jobs are tenant-scoped under RLS.

---

## T6. UDP "not served yet" state (new gap, from ADR 0013) — DONE — ship before UDP is offered in the UI

**Problem.** A `protocol: udp` virtual service is accepted and stored, but the HAProxy renderer skips it (a warning in the render result, `docs/render.md`) and nothing serves it until M6. Today the API never shows that warning, so the UI would show a service that looks configured but carries no traffic.

**Scope**
- Diff: add `warnings[]` next to `problems[]`: `{code, path, message}`. First code: `udp_not_served` ("UDP not served until M6"). Warnings never block an apply (a `problem` does). Carry the renderer's existing warnings into this list.
- Status: a UDP virtual service reports `state: "not_served"` (typed enum value, not `down`) until its engine renders and reports it.
- Document both in OpenAPI with an example (T2).

**Acceptance.** A change set that creates a UDP service applies, its diff lists the warning, and status shows `not_served`; golden test for the warning text.

---

## T7. UDP field rules and the `udp` health monitor (M6, ADR 0013, ADR 0020) — DONE

**Problem.** The UI forms need to know which fields a UDP service and its pool may use, and which monitor type to offer.

**Scope**
- `MonitorType` gains `udp` (optional payload and expected reply; no `http` block). The enum is in the OpenAPI schema, so the generated client gets it.
- Validator, with errors on the field (`errors[].path`): reject on a UDP service `certificate_ids`, `allow_list_id`, `enforce_allow_list`, TLS, `proxy_protocol`, `http2`, `https_redirect_port`, routes, policy sets, `restore_client_ip_header`. On pools used by UDP services reject `tls`, `persistence`, algorithm `random`, a `tcp`/`http` monitor, a member without a port; reject a pool shared by `tcp` and `udp` services; allow `udp` monitors only on UDP pools. Today only part of this exists (see 02, UDP section).
- Expose the allowed combinations so forms can disable fields: either `GET /api/v1/schema/virtual-service?protocol=udp` or `oneOf` by `protocol` (same approach as T4).

**Acceptance.** One test per rejected field with the expected `path`; OpenAPI shows the new enum value; the UI can build the UDP form from the schema alone.

---

## G1. Auth, RBAC and identity administration (spec: 02 "Auth and RBAC", ADR 0030)

T1 shipped OIDC login, sessions, `GET /me`, per-tenant roles, break-glass login and tenant tokens (ADR 0012). G1 is the rest the screens need. Order: G1-1 first (everything else reads the matrix), G1-2 and G1-3 next (SAML depends on both), then G1-4 to G1-7 in any order.

**Exists today (do not rebuild):** `GET /auth/providers`, `GET /auth/login/{provider}`, `GET /auth/callback/{provider}` (OIDC), `POST /auth/break-glass`, `POST /auth/logout`, `GET /me`, `GET/POST /tenants/{t}/tokens`, `DELETE /tenants/{t}/tokens/{id}`, CSRF header on cookie writes, `syctl idp add|map|list|enable`, `syctl local-admin`.

### G1-1. Permission matrix as code, `GET /permissions`, 403 detail

- `domain/permissions.py`: the matrix of 02 as data (`(verb, object) → minimum role`); services replace their `require_role(...)` literals with it. Behavior changes against today: job retry/cancel needs the role of the originating action; tokens cannot create tokens; `required_role` in the 403.
- `GET /api/v1/permissions` (any authenticated caller) → `[{verb, object, min_role}]`, plus the ordered `roles` list. The UI's `can(verb, object)` is `role_in(tenant) ≥ min_role`.
- 403 and 401 problems gain a `code` member: `session_expired`, `csrf`, `role_required` (+ `required_role`), `user_disabled`.
- **Acceptance.** A parametrized test calls every route with a token of each role and compares the status with the matrix; a test fails when the table in 02, the code and `/permissions` disagree; OpenAPI documents `code` and `required_role`.

### G1-2. Sessions: refresh, grants recomputation, `/me` additions

- Lifetimes 4 h absolute / 30 min idle (break-glass 1 h / 15 min). Grants recomputed from stored groups and current mappings when older than 60 s; session revoked if no grant is left.
- `POST /auth/refresh` (CSRF required) → the `Me` payload after recomputation; it does not extend the absolute lifetime.
- `GET /me` adds: `session: {expires_at, idle_timeout_seconds, grants_computed_at}` and per tenant `via: [{provider, group}]` (empty for break-glass and tokens). Existing fields unchanged: `id`, `name`, `email`, `is_system`, `break_glass`, `auth`, `csrf_token`, `tenants[{id, name, role}]`.
- Failed browser logins redirect to `/login?error=<code>` (`provider_error`, `expired`, `no_access`, `disabled`).
- **Acceptance.** Changing a mapping changes `/me` within 60 s without a new login, and immediately after `/auth/refresh`; removing the last mapping revokes the session (next call 401 `session_expired`); the callback failure paths redirect instead of returning JSON.

### G1-3. Identity-provider API and test sign-in

- System-admin: `GET/POST /identity-providers`, `GET/PATCH/DELETE /identity-providers/{id}` (`If-Match` on `version`); fields in 02. `protocol` selects the schema (OIDC or SAML) as in T4. Secrets are references or write-only; reads never return them.
- `POST /identity-providers/{id}/test` → `{test_id, authorize_url}`; `GET /identity-providers/{id}/test/{test_id}` → `{status: pending|done, subject, username, email, groups, claims, would_grant[], warnings[]}`. The callback creates no session for a test; the result is readable once, within five minutes.
- Validation errors name the field (`errors[].path`); an OIDC provider is probed on create (discovery document reachable) and the result is reported as a warning, not a refusal.
- **Acceptance.** Test sign-in against the fake OIDC provider returns claims and `would_grant`, creates no `user_session` row, and a second read is 404; no response contains a client secret or token.

### G1-4. SAML sign-in and service-provider metadata (built, ADR 0018; `sign_requests` and metadata import remain)

- `protocol: saml` accepted. `GET /auth/login/{provider}` redirects with an `AuthnRequest`; `POST /auth/callback/{provider}` is the assertion consumer service; `GET /auth/saml/{provider}/metadata` returns SP metadata (`application/samlmetadata+xml`, public; the UI's download button uses it, with `Content-Disposition` when `?download=1`).
- Rules in 02: signature required, `InResponseTo` bound to the stored state, assertion replay cache, 60 s skew, no IdP-initiated sign-in, groups from `attribute_groups`.
- **Acceptance.** Round trip against a test IdP (signed response accepted; unsigned, replayed, wrong `InResponseTo`, expired and wrong-audience refused, each audited as `auth.login_failed`); the metadata validates against the SAML schema and lists the ACS URL.

### G1-5. Role mappings

- System-wide `GET/POST /role-mappings`, `GET/PATCH/DELETE /role-mappings/{id}`; per tenant `GET/POST /tenants/{t}/role-mappings`, `PATCH/DELETE …/{id}`. Fields and who may use which in 02 (tenant-admin: `viewer` and `operator`, own tenant only). `If-Match` on `version`. Duplicate (`provider`, `group`, `tenant`) → 409.
- Remove `oidc_group` from tenant create, update and read, from `examples.py` and from the model; migration logs old values to the audit log (`tenant.oidc_group_dropped`) then drops the column. Update `syctl` accordingly; `syctl idp map` keeps working and writes the same rows.
- **Acceptance.** A tenant-admin gets 403 `role_required` creating a `tenant-admin` mapping and 404 touching another tenant's mapping; the OpenAPI breaking-change check is allowed for `oidc_group` in this MR only; audit events for each change.

### G1-6. Users

- `user_account` upserted at every sign-in. System-admin: `GET /users`, `GET /users/{id}` (with `effective_grants`), `POST /users/{id}/disable|enable|revoke-sessions`, `DELETE /users/{id}`. Tenant-admin: `GET /tenants/{t}/users`. Details and field list in 02.
- `user_session.user_id` links sessions; `revoke-sessions` ends them at once; a disabled user is refused at sign-in (`/login?error=disabled`) and by `authenticate`.
- Cursor pagination and `q` search as in ADR 0014. The break-glass administrator has no `user_account` row (its sessions are audited, not listed).
- **Acceptance.** Sign in twice: one row, `last_login_at` moves, `first_login_at` does not; no secret appears in any column; a tenant-admin sees only people with a grant in their tenant and only that tenant's grants.

### G1-7. Break-glass administration and system tokens

- `GET /system/break-glass`, `PUT /system/break-glass`, `POST /system/break-glass/admin`, `POST /system/break-glass/admin/confirm`, `DELETE /system/break-glass/admin` (system-admin; behavior in 02). The password is generated and shown once; the TOTP secret goes to OpenBao and is shown once as `totp_secret` and `otpauth_uri`.
- `/auth/providers` reports `break_glass: true` only when it is on (forced, or enabled and not expired).
- `GET/POST /system/tokens`, `DELETE /system/tokens/{id}` (30-day default, 90-day maximum); the tenant routes keep refusing `system-admin` tokens with a clear 422.
- **Acceptance.** Enrolling and confirming makes a login possible; an unconfirmed admin cannot log in; enabling needs a reason and expires on time (test with the injected clock); `SWITCHYARD_BREAK_GLASS=1` shows `forced_by_deployment: true` and cannot be turned off through the API; all of it is audited.

### What the Claude Design mockups assume, and what changes

Only the first two rows come from Claude Design's stated assumptions (highest role wins, next refresh). The others are places where the spec differs from the likely screens; check each against the mockups.

| Mockup says | Spec says | Change in the mockups |
| --- | --- | --- |
| Highest role wins when several groups match | True (roles nest, so the union is the highest role) | None to the rule. Add the "granted by" list (`via`) to the user and "my access" views so the result can be explained. |
| Role changes apply at next refresh | Within 60 s without signing out (or at once via refresh); changes of group membership at the provider need a new sign-in (at most 4 h) | Reword to "within a minute"; add a Refresh access action; admin screens need *Revoke sessions* and *Disable user*. |
| Sessions last 8 h | 4 h absolute, 30 min idle (break-glass 1 h / 15 min) | Session-expiry banner and silent re-login path; show expiry from `/me.session`. |
| Tenant form has an OIDC group field | Removed | Delete the field; point to the role-mappings tab. |
| Approver is a separate permission (design 02 said so) | Tenant-admin only, never the author or requester | Disable Approve for operators with the tooltip "needs tenant-admin"; show "you wrote this" when the viewer is the author. |
| Operators may do everything except admin | Operators cannot touch networks, VIP pools, SE groups, routing profiles, engines, deployments, tokens or mappings; job retry/cancel only for their own action | Disabled controls per the matrix; take them from `GET /permissions`, not from hard-coded rules. |
| Tokens in one place | Tenant tokens under the tenant; system tokens (system-admin) on a separate admin page; a token cannot create tokens | Split the tokens screen; tenant roles list stops at tenant-admin. |
| Login errors shown on the login page as text | Redirect to `/login?error=<code>` | Four error states: provider error, expired, no access, disabled. |
| Provider form (OIDC) | OIDC and SAML forms by `protocol`, secret write-only, test sign-in with claims and "would grant" | SAML form, SP-metadata download, test result panel with `warnings`. |
| Users screen (if any) | `user_account` list with last sign-in, groups, disable, effective grants | New list and detail screens (system-admin); a members list in tenant settings. |
| Break-glass is a toggle | Timed enablement with reason, TOTP enrollment (QR from `otpauth_uri`, password shown once, confirm step); forced-by-deployment is read-only | Enrollment wizard and a countdown when enabled. |

---

## Gap audit of 2026-10-10 (origin/main against 04)

04 was written against M1–M3. Re-audited against `main` after M6 and SAML. **Closed since 04:** typed responses (T2), rendered config (T3), policy/routing unions (T4), jobs list/stages/log/retry/cancel (T5), diff `warnings` (T6), UDP rules and monitor (T7), certificate import/issue/renew, SE group deploy and engine retire, network SDN apply, enrollment tokens, `/me`, login (OIDC, SAML, break-glass), `status/virtual-services`, `?change_set=` drafts, cursor pagination on change sets and revisions. **G1 identity administration** (providers, mappings, users, system tokens, refresh, permissions) is owned by the G1 tickets above and is not repeated here.

Still missing for a UI build, one ticket each (status is updated in this table as they land). All of A1–A12 except A3 landed with ADR 0040; the Claude Design follow-ups (items 3–14 of its request) are folded in.

| Id | Gap | Ticket | Status |
| --- | --- | --- | --- |
| A1 | Audit log has only `limit`: no filter by actor, action, object, time, no cursor | `GET …/audit-events?actor&action&object&since&until&q&cursor&limit` returning `Page` | done |
| A2 | No IP usage or allocations for VIP pools; no "release" | `GET …/vip-pools/{id}/allocations` (total, used, free, holders with draft status, `next_free`), `POST …/vip-pools/{id}/next-free` (suggest an address). VIPs are held by the service that carries them, so *release* means changing or deleting that service; there is no separate release call | done (ADR 0040) |
| A3 | Health monitor "test against one member" | `POST …/health-monitors/{id}/test` `{pool_id, member_id}` → `{ok, latency_ms, detail}` | open |
| A4 | Tenant search for the command palette | `GET …/search?q&types` over all object names | done |
| A5 | Tenant home needs one call, not eight | `GET …/summary` (service states, failing members, drifted engines, expiring certificates, running jobs, open drafts, latest revision) | done |
| A6 | Engines: only per-group list, thin read model | `GET …/engines`, `GET …/engines/{id}` with `mgmt_ip`, `agent_url`, `se_group_id`, enrollment state, VM placement | done (host online state not exposed) |
| A7 | List filters and sort: `?se_group=`, `?protocol=`, `?enabled=`, `?sort=` | on virtual services, pools, engines. **`?label=` is dropped: objects have no labels** (decision recorded in 04) | done |
| A8 | Change-set conflicts and impact (G8) | Stale `expected_version` returns `412` with `conflict{object_type, object_id, expected_version, current_version}` (client holds base, `GET` gives theirs); diff gains `impact` (services, VIPs, SE groups, hostnames) | done |
| A9 | Metrics and logs (G4) | `Telemetry` adapter (Prometheus, Loki) behind `GET …/virtual-services/{id}/metrics` and `…/logs`; both answer `available: false` when not configured | done (API; the telemetry pipeline is not built) |
| A10 | Finer live events (G5) | SSE `job_progress`, `vs_state`, `revision_applied` next to `status`; catalog under `x-sse-events` in OpenAPI (`member_status` and `bgp_session` are covered by `status`) | done |
| A11 | OpenAPI completeness | Every route: summary, description, request and response models, example, error responses with problem codes, auth and role note, pagination note. A test fails when one is missing. ReDoc beside Swagger UI; `docs/api/openapi.json` checked | done |
| A12 | Docs match reality | `docs/api.md`, `docs/operating.md`, README, 04 (mark anything without an API) | done |
| A13 | `routes_withdrawn` is one boolean but withdrawal is per prefix (ADR 0020); Design asked for the list | Agent reports `withdrawn_prefixes[]`; status gains `agent.withdrawn_prefixes` | open |
| A14 | Design asked for enumerated state values (BGP, VRRP, IPVS, HAProxy member status), job kinds, the audit action catalog, apply-under-approval behaviour, retire vs remove, deploy and tokens | Documented in `docs/api.md`, 04 and ADR 0040; `JobKind` enum and `GET /audit-actions` in OpenAPI | done |

## Follow-ups (not blocking U1; ticket when the milestone starts)

| Gap | Ticket | Milestone |
| --- | --- | --- |
| G4 | Metrics and logs endpoints (Prometheus/Loki adapter, tenant-label enforced) + Grafana proxy | U2 — API done; telemetry pipeline open |
| G5 | Finer SSE events (`member_status`, `vs_health`, `bgp_session`, `revision_applied`) and a typed catalog | U2 — done |
| G8 | Conflict (three-way) response on stale `expected_version`; diff "impact" section | U1/U2 — done |
| G9 | VIP allocations/usage/release; health-monitor test; cert issue/renew; SE deploy | U3–U4 — VIP allocations, cert issue/renew, SE deploy done; monitor test open (A3) |
| G10 | `?label=`, `?se_group=` filters, sorting, audit filters/cursor | U1 — done |
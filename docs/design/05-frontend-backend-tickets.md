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

## T6. UDP "not served yet" state (new gap, from ADR 0013) — ship before UDP is offered in the UI

**Problem.** A `protocol: udp` virtual service is accepted and stored, but the HAProxy renderer skips it (a warning in the render result, `docs/render.md`) and nothing serves it until M6. Today the API never shows that warning, so the UI would show a service that looks configured but carries no traffic.

**Scope**
- Diff: add `warnings[]` next to `problems[]`: `{code, path, message}`. First code: `udp_not_served` ("UDP not served until M6"). Warnings never block an apply (a `problem` does). Carry the renderer's existing warnings into this list.
- Status: a UDP virtual service reports `state: "not_served"` (typed enum value, not `down`) until its engine renders and reports it.
- Document both in OpenAPI with an example (T2).

**Acceptance.** A change set that creates a UDP service applies, its diff lists the warning, and status shows `not_served`; golden test for the warning text.

---

## T7. UDP field rules and the `udp` health monitor (M6, ADR 0013)

**Problem.** The UI forms need to know which fields a UDP service and its pool may use, and which monitor type to offer.

**Scope**
- `MonitorType` gains `udp` (optional payload and expected reply; no `http` block). The enum is in the OpenAPI schema, so the generated client gets it.
- Validator, with errors on the field (`errors[].path`): reject on a UDP service `certificate_ids`, `allow_list_id`, `enforce_allow_list`, TLS, `proxy_protocol`, `http2`, `https_redirect_port`, routes, policy sets, `restore_client_ip_header`. On pools used by UDP services reject `tls`, `persistence`, algorithm `random`, a `tcp`/`http` monitor, a member without a port; reject a pool shared by `tcp` and `udp` services; allow `udp` monitors only on UDP pools. Today only part of this exists (see 02, UDP section).
- Expose the allowed combinations so forms can disable fields: either `GET /api/v1/schema/virtual-service?protocol=udp` or `oneOf` by `protocol` (same approach as T4).

**Acceptance.** One test per rejected field with the expected `path`; OpenAPI shows the new enum value; the UI can build the UDP form from the schema alone.

---

## Follow-ups (not blocking U1; ticket when the milestone starts)

| Gap | Ticket | Milestone |
| --- | --- | --- |
| G4 | Metrics and logs endpoints (Prometheus/Loki adapter, tenant-label enforced) + Grafana proxy | U2 |
| G5 | Finer SSE events (`member_status`, `vs_health`, `bgp_session`, `revision_applied`) and a typed catalog | U2 |
| G8 | Conflict (three-way) response on stale `expected_version`; diff "impact" section | U1/U2 |
| G9 | VIP allocations/usage/release; health-monitor test; cert issue/renew; SE deploy; identity providers + role mappings | U3–U4 |
| G10 | `?label=`, `?se_group=` filters, sorting, audit filters/cursor | U1 |

# Switchyard — Frontend Scope and Claude Design Handoff

Date: 2026-10-10 (re-audited against `main` after M6, SAML login and ADR 0040). Companion to
[03-frontend-design.md](03-frontend-design.md) (stack, UX principles, design system) and
[02-backend-design.md](02-backend-design.md). **Design source:** the Claude Design canvas is the source of truth for
visuals; this document is the source of truth for data, states and endpoints.

This doc does two things: (1) says what the implemented controller API covers for each screen, and (2) gives Claude
Design everything per screen: entities and fields, states, interactions and endpoints.

**Sources.** `docs/api/openapi.json` (also served at `/api/v1/openapi.json`, Swagger UI `/api/v1/docs`, ReDoc
`/api/v1/redoc`), `docs/api.md`, ADRs 0006–0020, 0040, `domain/models.py`.

Legend: ✅ implemented and in OpenAPI · ⚠️ implemented with a stated limit · ❌ not backed by an API (design it, mark it
"blocked on API") · G1 (identity providers, mappings, users, system tokens, break-glass, permissions) is ✅ since ADR 0030–0032.

---

## 1. Verdict

**The API is ready to design and build every screen against, except the items in the table below.** Every operation in
OpenAPI has a typed request and response, an example, its error responses and its required role; reads are typed
(no `unknown`), lists are paginated, writes go through change sets.

| Area | Status |
| --- | --- |
| Login (OIDC, SAML, break-glass), session cookie + CSRF, `GET /me` | ✅ |
| Identity providers (OIDC, SAML, test sign-in), role mappings, users, system tokens, `GET /permissions`, `POST /auth/refresh`, break-glass administration | ✅ (G1, ADR 0030–0032; spec in 05) |
| Typed reads, `ETag`, `If-Match`, problem+json with field paths, `conflict` on stale versions | ✅ |
| Rendered config per object, typed diff with files, `warnings`, `impact` | ✅ |
| Jobs: list, stages, log, retry, cancel, kind enum | ✅ |
| Virtual service health (`healthy/degraded/down/unknown/disabled`) and tenant summary | ✅ |
| Metrics (RPS, p95, 5xx, sessions) and access-log tail | ⚠️ API ✅; returns `available: false` until Prometheus/Loki and the SE telemetry pipeline exist (ADR 0040) |
| Live events (`status`, `vs_state`, `job_progress`, `revision_applied`) | ✅ (server polls the database; reconnect restarts from a snapshot) |
| VIP pool usage, allocations, next free address | ✅ (no reservation; *release* = edit or delete the service) |
| Audit filters, cursor, cross-tenant log, action catalog, object history | ✅ |
| Search for the command palette | ✅ |
| Engines: tenant list, placement (`pve_node`, `vmid`), enrollment state | ✅ (host online state ❌) |
| Certificates: import (PEM), issue (ACME, Origin CA), renew | ✅ (jobs; result carries the `system:certs` change set) |
| SE group deploy, engine retire, network SDN apply | ✅ |
| Health monitor "test against one member" | ✅ `POST health-monitors/{id}/test` (runs on an engine through the agent; needs an upgraded agent and a member already applied) |
| Per-prefix BGP withdrawal list | ✅ `agent.withdrawn_prefixes[]` (ADR 0041) |
| `?label=` filters | ❌ dropped: objects have no labels |

**Rules that came out of the audit** (details in ADR 0040): a VIP is held by the service that carries it; apply on a
tenant that requires approval is `403` for everyone but a system administrator with a reason; *Retire* destroys the
VM and the record, *Remove* only the record; deploy does not create enrollment tokens.

---

## 2. Cross-cutting behavior (applies to every screen)

**URL and tenant.** `/t/:tenant/...` everywhere except Login and Admin. Tenant name is in the top bar tenant switcher (`GET /tenants`). Switching tenant swaps all context.

**Standard states** (every screen shows all of these in the mockups):

| State | Treatment |
| --- | --- |
| Loading | Skeleton rows/cards; never a blank page or full-page spinner. |
| Empty | Short explanation plus the primary create action (e.g. "No pools yet. Create a pool."). Read-only users see the explanation without the action. |
| Error | Inline alert with the problem `title` and `detail`; "Retry". Field errors (`errors[].path`) mark the exact input. 401 → re-login modal that keeps unsaved input. 403 → read-only state with a tooltip naming the required role. 412 → "changed by someone else" with a reload action. |
| Draft | Open draft shows the change-set bar; edited objects and fields carry the `draft` dot; deleted objects shown struck through (`draft_status: deleted`). |
| Pending approval | Draft frozen; banner with approver and "Request approval" state; edit controls disabled. |
| Read-only | Missing permission: controls disabled with tooltip. |
| Live / stale | Live data shows `observed_at`; a banner when the SSE stream is down; stale data (engine `reachable: false`) shows last-seen time. |
| **Drift** | Engine `drift` set → warning chip "Drifted: running config differs from desired" on the SE and rolled up to the SE group, VS and tenant home. Drift is reported only; **never offer auto-fix** (project rule). Offer "Re-apply current revision" only as an explicit draft/runtime action. |

**Status vocabulary.** Reuse design tokens: `status-healthy`, `status-degraded`, `status-down`, `status-draining`, `draft`, `live`, `pending-approval`; each with an icon and a text label.

**Writes.** Edits never go live on save: they add an op to the open draft (open one implicitly on first edit). Runtime actions (member state, engine state, token create/revoke, cert renew) skip the change set, show a confirm dialog and a `Live` badge.

**Async.** Anything that returns `202` carries a `job_id`; show `JobProgress` from `GET /tenants/{t}/jobs/{id}` (stages) and the `job_progress` event of the stream.

**Optimistic locking.** Object PATCH sends `If-Match: "<version>"`; change-set ops carry `expected_version`. Stale → conflict flow (§3.13).

---

## 3. Screens

Each screen lists entities and fields (names are API field names), states beyond the standard ones, key interactions, and endpoints. All paths are under `/api/v1`. `{t}` = tenant. Roles are the minimum role from `x-required-role`; take enable/disable of controls from `GET /permissions` (`can(verb, object)`).

### 3.1 App shell
- **Regions:** top bar (tenant switcher, command palette trigger, user menu, theme toggle, SSE connection indicator), left section nav, bottom change-set bar (only with an open draft), toast area.
- **Change-set bar fields:** `summary` (editable name), `author`, `state`, op count, per-type counts, buttons Review & apply / Discard. States: draft, pending_approval ("Awaiting approver"), applying (progress), failed (error + "Open review").
- **Command palette:** `GET /tenants/{t}/search?q=&types=` returns best matches first (objects by name; services by VIP, certificates by SAN, pools by member address).
- **Endpoints:** `GET /tenants` ✅, `GET /tenants/{t}/change-sets?state=draft` ✅ (`base_revision`, `created_at`, `op_count`), `POST /tenants/{t}/change-sets` ✅, `GET /me` ✅, `GET /tenants/{t}/search` ✅, `GET /tenants/{t}/status/stream` ✅ (connection indicator = this stream).

### 3.2 Login
- **Shows:** enabled identity providers (name, protocol oidc|saml), optional break-glass link (password + TOTP).
- **States:** loading, no providers configured, provider error, session expired (returns to prior URL). Failed browser logins land on `/login?error=<code>` (`provider_error`, `expired`, `no_access`, `disabled`).
- **Endpoints:** `GET /auth/providers` ✅, `GET /auth/login/{provider}` ✅ (redirect), OIDC `GET` and SAML `POST /auth/callback/{provider}` ✅, `POST /auth/break-glass` ✅, `POST /auth/logout` ✅. Stand-in without a provider: "paste an API token" form (bearer auth).

### 3.3 Tenant home
- **Cards:** services by health (`services`), failing members, drifted engines, unreachable engines, certificates expiring within 30 days (with `days_left`), running/queued/failed jobs, open drafts and drafts awaiting approval, latest revision.
- **Interactions:** each item links to the object; "Jump to problems".
- **Endpoint:** `GET /tenants/{t}/summary` ✅ (one call, lists capped at 50). Detail behind each card: `status/engines`, `status/virtual-services`, `certificates?expires_within_days=30`, `jobs?state=`, `change-sets?state=`, `revisions`.

### 3.4 Virtual services list
- **Columns:** `name`, `vip`, `ports[].port`, `protocol`, `se_group_id` (resolve to name), `enabled`, `draft_status`, **health** (`status/virtual-services`: `health`, `members_up/members_total`), **RPS, 5xx** (`metrics/virtual-services`; ⚠️ `available: false` until telemetry exists, show "no data source"), cert expiry (derive from `certificate_ids` → `not_after`). p95 is on the detail page only.
- **Interactions:** filter by `q`, `se_group`, `protocol`, `enabled`, `pool`, `certificate`; sort `name|-name`; enable/disable (PATCH `enabled` → draft op); create (wizard); delete. No label filter (dropped).
- **States:** standard; plus "N unsaved changes" in draft mode and deleted-in-draft rows.
- **Endpoints:** `GET virtual-services?q&se_group&protocol&enabled&sort&change_set&cursor&limit` ✅, `PATCH virtual-services/{id}` ✅ (`If-Match`), `DELETE` ✅.

### 3.5 Virtual service detail (tabs: Overview, Settings, Rendered config, History)
- **Settings fields:** `name`, `enabled`, `protocol` (http|tcp|udp), `se_group_id`, `vip` or `vip_pool_id` (suggest with `POST vip-pools/{id}/next-free`), `ports[]` (`port`, `tls`, `http2`, `proxy_protocol`, `https_redirect_port`), `certificate_ids[]`, `default_pool_id`, `routes[]` (`match{host, path_prefix, path_exact}`, `pool_id` | `redirect{location, code}`), `policy_set_ids[]`, `allow_list_id`, `enforce_allow_list`, `restore_client_ip_header`. For UDP, take the disabled fields from `GET /schema/virtual-service?protocol=udp`.
- **Overview:** health chip and member counts, route table (host/path → pool), live member status per pool (from `status/engines` `members`), metrics charts (`GET virtual-services/{id}/metrics`: RPS, p95, 5xx ratio, sessions; ⚠️ `available: false` → "no data source") and access-log tail (`GET virtual-services/{id}/logs`; ⚠️ same), rendered frontend.
- **Rendered config tab:** `GET virtual-services/{id}/rendered[?change_set=]` ✅ (`file`, `language`, `content`, `fingerprint`, per SE group). With a draft it shows the draft's output; pair with the diff for before/after.
- **History tab:** `GET revisions?object={id}` ✅ (revisions that touched it); runtime events (member drains) are audit events of the pool: `GET audit-events?object=…` ⚠️ only for objects that audit with their own reference (engines, jobs, tokens, change sets).
- **Interactions:** edit fields (adds ops), add/reorder routes, drain a member (runtime, `Live`), enable/disable.
- **Validation messages to design:** VIP outside the SE group's networks, port collision on a VIP, cert SANs not covering a route host, route with both pool and redirect, route with neither (field paths come back in `errors[].path`).
- **Endpoints:** as above plus `GET/PATCH virtual-services/{id}?change_set=` ✅, `PUT pools/{pool}/members/{member}/state` ✅.

### 3.6 Create virtual service wizard
Steps: basics (`name`, `protocol`, `se_group_id`) → VIP (`vip_pool_id` pick or fixed `vip`; show pool usage from `vip-pools/{id}/allocations`) → ports and TLS (`ports[]`, `certificate_ids[]`) → routes and pools (`routes[]`, `default_pool_id`, create pool inline) → health check (pick/create monitor) → review (field summary + validator problems) → "Add to change set".
- **States:** per-step validation, back/next retains input, no VIP pools available (empty with link to Networks and VIP pools), no certs for TLS (link to import), pool exhausted (`409` from `next-free`).
- **Endpoints:** `POST virtual-services?change_set=` ✅ (returns `Accepted{change_set_id, object_id, op_index}`); inline pool/monitor creation uses additional ops via `POST change-sets/{cs}/ops` ✅ (atomic multi-object). Pre-flight validation without saving: the diff `problems` ✅.

### 3.7 Pools
- **Fields:** `name`, `algorithm` (roundrobin|leastconn|source|random), `health_monitor_id`, `tls`, `tls_verify`, `persistence` (`type`, `table_size`, `timeout_s`), `members[]` (`id`, `address`, `port`, `weight`, `admin_state` enabled|disabled|drain).
- **Live per member:** `members[]` of `status/engines`: `backend` (`pool_<name>`), `server`, `status` (`UP`, `DOWN`, `DRAIN`, `MAINT`, `NOLB`; may carry a suffix such as `UP 1/2`; treat anything starting with `UP` as up). Chip map: `UP` healthy, `DRAIN` draining, `MAINT`/`NOLB` disabled, `DOWN` down.
- **Interactions:** add/remove/edit members (draft), enable/disable/drain at runtime (`Live`, confirm dialog, response `{state, applied, failed}` lists per-engine results → show partial failure), edit pool. Filter pools by `health_monitor`.
- **Endpoints:** CRUD ✅; `PUT pools/{pool}/members/{member}/state` ✅.

### 3.8 Health monitors
- **Fields:** `name`, `type` (tcp|http|udp), `interval_s`, `timeout_s`, `rise`, `fall`, `port`, `http{method GET|HEAD|OPTIONS, path, host, expect_status[]}` (http only); "pools using it" (`GET pools?health_monitor={id}`).
- **Interactions:** edit; **Test against one member:** `POST health-monitors/{id}/test` ✅ with `{pool_id, member_id, engine_id?, port?, http_path?, expect_status?, timeout_s?}` → `{ok, latency_ms, detail, engine, target, protocol}`; saves nothing. States: running, pass, fail (detail), refused (`409`: member not applied yet), agent unreachable (`502`).
- **Endpoints:** CRUD ✅.

### 3.9 Certificates
- **Fields:** `name`, `source` (upload|cloudflare_origin|acme|step_ca), `sans[]`, `not_after`, `renew_policy` (manual|auto), `secret_ref` (an OpenBao reference; reads never return key material), "VSes using it" (`GET virtual-services?certificate={id}`).
- **Expiry presentation:** ≤30 days warning, ≤7 danger, expired down; icon and text with each. Filter `expires_within_days`.
- **Interactions:** **import** (name + PEM chain and key; the PEM is sent once to the controller, which stores the key in OpenBao and keeps only the reference — say so in the dialog; never echo it back), **issue** (ACME or Cloudflare Origin CA, `sans[]`), **renew now**, edit renew policy, delete (blocked when in use; show which services). Issue and renew return `202 {job_id}`; the job's result carries `change_set_id` of the `system:certs` change set that records the certificate, so link "Review the change set". Import needs OpenBao: without a secrets backend it answers `503` (show the reason). Issue and renew queue a job; when ACME, Cloudflare or OpenBao is missing the job ends `failed`/`dead` with the reason in `error`.
- **Endpoints:** CRUD ✅; `POST certificates/import`, `POST certificates/issue`, `POST certificates/{id}/renew` ✅ (operator).

### 3.10 Policies
- **Kinds** (shapes in §5): `redirect` (rules: match + redirect), `allow_list` (CIDRs, optional `managed_by: cloudflare`).
- **UI:** list with `kind` filter; rule builder for redirects; CIDR list editor with validation for allow lists; Cloudflare-managed lists are read-only with a "Managed by Cloudflare sync" badge.
- **Endpoints:** CRUD ✅ (typed `oneOf` on `kind`).

### 3.11 Networks and VIP pools
- **Network fields:** `name`, `cidr`, `gateway`, `sdn_zone`, `vnet`, `vlan_or_vni`, `managed` (created by controller vs attached).
- **VIP pool fields:** `name`, `network_id`, `ranges[]` (`start`, `end`). **Usage:** `GET vip-pools/{id}/allocations` ✅ → `total`, `used`, `free`, `allocations[]` (address, service, `enabled`, `draft_status`), `next_free`. **Release** is not a call: an address is held by the service that carries it, so release = change or delete that service in a change set. `POST vip-pools/{id}/next-free` suggests an address (nothing reserved).
- **States:** pending SDN apply (job), network in use (delete blocked), pool exhausted.
- **Endpoints:** CRUD ✅ (both; tenant-admin); `POST networks/apply-sdn` ✅ (202 job; managed networks only).

### 3.12 SE groups, engines, routing profiles
- **SE group fields:** `name`, `ha_mode` (bgp|vrrp), `networks[]` (`network_id`, `role` vip|backend|mgmt), `maxconn`, `canary_seconds`, `routing_profile_id`. Filter `ha_mode`.
- **Engines (SEs):** `GET engines` (all) / `GET se-groups/{g}/engines`; fields `id`, `name`, `url`, `state`, `se_group_id`, `mgmt_ip`, `agent_url`, `pve_node`, `vmid`, `agent_enrolled`, `agent_cert_serial`. Live per engine from `status/engines`: `reachable`, `error`, `haproxy_fingerprint`, `drift[]`, `observed_at`, `agent{version, bundle_id, haproxy_healthy, routes_withdrawn, bgp[], vrrp[], ipvs[]}`.
- **State values for chips:** BGP session `state` (FRR): `Idle`, `Connect`, `Active`, `OpenSent`, `OpenConfirm`, `Established` (only `Established` is healthy); VRRP `state`: `MASTER`, `BACKUP`, `FAULT`; IPVS real server `state`: `up`, `down`, `drain`. `routes_withdrawn` is true while any prefix is withdrawn; `withdrawn_prefixes[]` lists them.
- **Interactions:** register (hand-built), enable/disable (runtime, `Live`), **Remove** (record only, VM keeps running), **Retire** (destroys the VM and removes the record; refused for the last serving engine; job), **Deploy** (`count`, `nodes`, `template_vmid`, `mgmt_pool_id`, optional `ssh_public_keys` for people to log in to the VM — the controller never uses SSH; job), **Create enrollment token** per engine (deploy does not create them; show the `sy-agent enroll` command). Host online state ❌.
- **Routing profiles** (shapes in §5): VRRP profile or BGP profile with peers. Filter `kind`.
- **Endpoints:** SE groups CRUD ✅; engines `GET/POST se-groups/{g}/engines`, `GET engines`, `GET engines/{id}`, `PUT engines/{id}/state?state=`, `DELETE engines/{id}`, `POST engines/{id}/retire`, `POST engines/{id}/enrollment-token`, `POST se-groups/{g}/deploy` ✅ (tenant-admin); `GET status/engines` ✅; routing profiles CRUD ✅.

### 3.13 Change sets: review, apply, conflicts, approval
Screens: list (open drafts, history), full-screen review (tabs Objects / Config / Impact), apply progress, conflict resolver, approval banner.
- **List fields:** `id`, `summary`, `author`, `state` (draft|pending_approval|applying|applied|failed|discarded), `base_revision`, `created_at`, `op_count`. Detail adds `approver`, `reject_comment`, `error`, `ops[]` (`action` create|update|delete, `object_type`, `object_id`, `body`, `expected_version`).
- **Review / Objects:** `diff.objects[]` (`type`, `id`, `name`, `status`, `changed_fields`); before/after values come from the live and draft reads. **Config:** `diff.se_groups[]` (`id`, `name`, `error`, `files[]` with `before`, `after`, unified `diff`). **Impact:** `diff.problems[]` (block), `diff.warnings[]` (never block), `diff.impact` (`services[]`, `vips[]`, `se_groups[]`, `hostnames[]`).
- **Apply:** `POST …/apply` → `202 {job_id}`; optional body `{break_glass_reason}` for a system administrator on a tenant that requires approval. **When the tenant has `require_approval` and the caller is not a system administrator, the call answers `403`** — hide Apply (from `tenant.require_approval` and the caller's role) and offer "Request approval". Progress: `GET jobs/{id}` stages lock, render, validate, canary, rollout, commit, or the `job_progress` event; failure shows the failed stage and `error`; the draft stays open.
- **Approval:** "Request approval" → `pending_approval`; a tenant administrator other than the author or requester sees Approve (applies) / Reject (required `comment`, reopens the draft). Show approver and `reject_comment` in the banner. Edits after the request void it.
- **Conflicts:** a stale `expected_version` answers `412` with `conflict{object_type, object_id, expected_version, current_version}`. Three-way view: *base* = what the client had read, *theirs* = `GET` of the object, *yours* = the operation `body`; resolve and resend with `expected_version: current_version`.
- **Endpoints:** all ✅: `GET/POST change-sets`, `GET change-sets/{cs}`, `POST …/ops`, `DELETE …/ops/{index}`, `GET …/diff`, `POST …/apply|discard|request-approval|approve|reject`.

### 3.14 Revisions
- **Fields:** `number`, `author`, `summary`, `git_sha`, `created_at`; detail adds `snapshot` (full desired state).
- **Interactions:** diff any two (client-side from two snapshots), history of one object (`?object={id}`), "Roll back to #N" → `POST revisions/{n}/rollback` returns a pre-filled change set (`201`) and opens the standard review (tenant-admin).
- **Endpoints:** `GET revisions[?object=]` ✅ (paged), `GET revisions/{n}` ✅, `POST revisions/{n}/rollback` ✅.

### 3.15 Jobs
- **List fields:** `id`, `kind` (enum: `apply_change_set`, `deploy_se_group`, `retire_engine`, `apply_sdn`, `issue_cert`, `renew_certs`, `sync_cloudflare_ips`, `poll_status`, `git_mirror`, `gc`), `state` (queued|running|succeeded|failed|dead|cancelled), `change_set_id`, `created_at`, `started_at`, `finished_at`, `attempts/max_attempts`, `retrying`, `ok`, `error`.
- **Detail:** `stages[]` (name, state, timing, error), typed `result` (apply: `ok`, `revision_number`, per-group results; `issue_cert`: `change_set_id`), log (`GET jobs/{id}/log`, latest 500 lines). A reverted or rejected apply is a *finished* job with `ok: false`, not a failed one (ADR 0007): show "Reverted" distinct from "Retrying after infrastructure error".
- **Actions:** retry a `dead` job, cancel a `queued` job (operator; `409` otherwise). Live updates: `job_progress` event.
- **Endpoints:** `GET jobs?state&kind&cursor&limit`, `GET jobs/{id}`, `GET jobs/{id}/log`, `POST jobs/{id}/retry|cancel` ✅.

### 3.16 Admin and access
- **Tenants (system-admin):** `name`, `quotas`, `require_approval` toggle (audited). `GET/POST /tenants`, `GET/PATCH/DELETE /tenants/{t}` ✅. Access is granted by role mappings (✅), not by a tenant field.
- **Tenant access and settings (tenant-admin):** members (`GET tenants/{t}/users`) and role mappings (`…/role-mappings`) ✅; tokens ✅.
- **Tenant API tokens:** list `id`, `name`, `role`, `revoked`, `expires_at`; create (`name`, `role`, `ttl_days`) returns the plaintext `token` **once** — copy-once dialog. Revoke is runtime + confirm. ✅. **System tokens** (separate admin page): `GET/POST /system/tokens`, `DELETE /system/tokens/{id}` ✅.
- **Audit log:** tenant view `GET tenants/{t}/audit-events` and system view `GET /audit-events?tenant=` (system-admin): filters `actor`, `action` (`change_set.*` prefix), `object`, `since`, `until`, `q`; cursor; each event has `at`, `tenant_id`, `actor`, `action`, `object`, `diff`, `request_id`. Labels and filter options from `GET /audit-actions` (action, category, description). ✅
- **Users, identity providers, role mappings, break-glass administration, system tokens:** ✅ (`/users`, `/identity-providers`, `/role-mappings`, `/system/break-glass`, `/system/tokens`; field lists and rules in 05 G1-1…G1-7 and ADR 0030–0032).

---

## 4. Endpoint inventory (implemented)

Base `/api/v1`. Generated from `docs/api/openapi.json`; the *Role* column is the minimum role (`public` needs none).
Errors are `application/problem+json` (`type`, `title`, `status`, `detail`, `errors[{path, code, message}]`, and
`conflict` on a stale version).

| Area | Method and path | Role | What it does |
| --- | --- | --- | --- |
| meta | `GET /healthz` | public | Liveness check |
| meta | `GET /schema/virtual-service` | authenticated | Field rules of a virtual service protocol |
| auth | `POST /auth/break-glass` | public | Log in the break-glass administrator |
| auth | `GET /auth/callback/{provider}` | public | Finish a login |
| auth | `POST /auth/callback/{provider}` | public | SAML assertion consumer service |
| auth | `GET /auth/login/{provider}` | public | Start a login |
| auth | `POST /auth/logout` | authenticated | Log out |
| auth | `GET /auth/providers` | public | List login providers |
| auth | `POST /auth/refresh` | authenticated | Recompute my roles now |
| auth | `GET /auth/saml/{provider}/metadata` | public | SAML service-provider metadata |
| auth | `GET /me` | authenticated | Who am I |
| auth | `GET /permissions` | authenticated | The permission matrix |
| tenants | `GET /tenants` | authenticated | List tenants |
| tenants | `POST /tenants` | system-admin | Create a tenant |
| tenants | `DELETE /tenants/{t}` | system-admin | Delete a tenant |
| tenants | `GET /tenants/{t}` | viewer | Get a tenant |
| tenants | `PATCH /tenants/{t}` | tenant-admin | Change a tenant |
| networks | `GET /tenants/{t}/networks` | viewer | List networks |
| networks | `POST /tenants/{t}/networks` | tenant-admin | Create one of the networks |
| networks | `DELETE /tenants/{t}/networks/{object_id}` | tenant-admin | Delete one of the networks |
| networks | `GET /tenants/{t}/networks/{object_id}` | viewer | Get one of the networks |
| networks | `PATCH /tenants/{t}/networks/{object_id}` | tenant-admin | Change one of the networks |
| vip-pools | `GET /tenants/{t}/vip-pools` | viewer | List vip pools |
| vip-pools | `POST /tenants/{t}/vip-pools` | tenant-admin | Create one of the vip pools |
| vip-pools | `DELETE /tenants/{t}/vip-pools/{object_id}` | tenant-admin | Delete one of the vip pools |
| vip-pools | `GET /tenants/{t}/vip-pools/{object_id}` | viewer | Get one of the vip pools |
| vip-pools | `PATCH /tenants/{t}/vip-pools/{object_id}` | tenant-admin | Change one of the vip pools |
| vip-pools | `GET /tenants/{t}/vip-pools/{pool_id}/allocations` | viewer | Usage and allocations of a VIP pool |
| vip-pools | `POST /tenants/{t}/vip-pools/{pool_id}/next-free` | viewer | Suggest the next free address of a VIP pool |
| se-groups | `GET /tenants/{t}/se-groups` | viewer | List se groups |
| se-groups | `POST /tenants/{t}/se-groups` | tenant-admin | Create one of the se groups |
| se-groups | `DELETE /tenants/{t}/se-groups/{object_id}` | tenant-admin | Delete one of the se groups |
| se-groups | `GET /tenants/{t}/se-groups/{object_id}` | viewer | Get one of the se groups |
| se-groups | `PATCH /tenants/{t}/se-groups/{object_id}` | tenant-admin | Change one of the se groups |
| se-groups | `GET /tenants/{t}/se-groups/{object_id}/rendered` | viewer | Rendered config of a se group |
| routing-profiles | `GET /tenants/{t}/routing-profiles` | viewer | List routing profiles |
| routing-profiles | `POST /tenants/{t}/routing-profiles` | tenant-admin | Create one of the routing profiles |
| routing-profiles | `DELETE /tenants/{t}/routing-profiles/{object_id}` | tenant-admin | Delete one of the routing profiles |
| routing-profiles | `GET /tenants/{t}/routing-profiles/{object_id}` | viewer | Get one of the routing profiles |
| routing-profiles | `PATCH /tenants/{t}/routing-profiles/{object_id}` | tenant-admin | Change one of the routing profiles |
| virtual-services | `GET /tenants/{t}/metrics/virtual-services` | viewer | Headline metrics of every virtual service |
| virtual-services | `GET /tenants/{t}/virtual-services` | viewer | List virtual services |
| virtual-services | `POST /tenants/{t}/virtual-services` | operator | Create one of the virtual services |
| virtual-services | `DELETE /tenants/{t}/virtual-services/{object_id}` | operator | Delete one of the virtual services |
| virtual-services | `GET /tenants/{t}/virtual-services/{object_id}` | viewer | Get one of the virtual services |
| virtual-services | `PATCH /tenants/{t}/virtual-services/{object_id}` | operator | Change one of the virtual services |
| virtual-services | `GET /tenants/{t}/virtual-services/{object_id}/logs` | viewer | Access log of a virtual service |
| virtual-services | `GET /tenants/{t}/virtual-services/{object_id}/metrics` | viewer | Metrics of a virtual service |
| virtual-services | `GET /tenants/{t}/virtual-services/{object_id}/rendered` | viewer | Rendered config of a virtual service |
| pools | `GET /tenants/{t}/pools` | viewer | List pools |
| pools | `POST /tenants/{t}/pools` | operator | Create one of the pools |
| pools | `DELETE /tenants/{t}/pools/{object_id}` | operator | Delete one of the pools |
| pools | `GET /tenants/{t}/pools/{object_id}` | viewer | Get one of the pools |
| pools | `PATCH /tenants/{t}/pools/{object_id}` | operator | Change one of the pools |
| pools | `GET /tenants/{t}/pools/{object_id}/rendered` | viewer | Rendered config of a pool |
| pools | `PUT /tenants/{t}/pools/{pool}/members/{member}/state` | operator | Set a pool member's runtime state |
| health-monitors | `GET /tenants/{t}/health-monitors` | viewer | List health monitors |
| health-monitors | `POST /tenants/{t}/health-monitors` | operator | Create one of the health monitors |
| health-monitors | `POST /tenants/{t}/health-monitors/{monitor_id}/test` | operator | Test a health monitor against one pool member |
| health-monitors | `DELETE /tenants/{t}/health-monitors/{object_id}` | operator | Delete one of the health monitors |
| health-monitors | `GET /tenants/{t}/health-monitors/{object_id}` | viewer | Get one of the health monitors |
| health-monitors | `PATCH /tenants/{t}/health-monitors/{object_id}` | operator | Change one of the health monitors |
| health-monitors | `GET /tenants/{t}/health-monitors/{object_id}/rendered` | viewer | Rendered config of a health monitor |
| certificates | `GET /tenants/{t}/certificates` | viewer | List certificates |
| certificates | `POST /tenants/{t}/certificates` | operator | Create one of the certificates |
| certificates | `POST /tenants/{t}/certificates/import` | operator | Import a certificate and key |
| certificates | `POST /tenants/{t}/certificates/issue` | operator | Issue a certificate (ACME or Cloudflare Origin CA) |
| certificates | `POST /tenants/{t}/certificates/{certificate}/renew` | operator | Renew a certificate |
| certificates | `DELETE /tenants/{t}/certificates/{object_id}` | operator | Delete one of the certificates |
| certificates | `GET /tenants/{t}/certificates/{object_id}` | viewer | Get one of the certificates |
| certificates | `PATCH /tenants/{t}/certificates/{object_id}` | operator | Change one of the certificates |
| certificates | `GET /tenants/{t}/certificates/{object_id}/rendered` | viewer | Rendered config of a certificate |
| policy-sets | `GET /tenants/{t}/policy-sets` | viewer | List policy sets |
| policy-sets | `POST /tenants/{t}/policy-sets` | operator | Create one of the policy sets |
| policy-sets | `DELETE /tenants/{t}/policy-sets/{object_id}` | operator | Delete one of the policy sets |
| policy-sets | `GET /tenants/{t}/policy-sets/{object_id}` | viewer | Get one of the policy sets |
| policy-sets | `PATCH /tenants/{t}/policy-sets/{object_id}` | operator | Change one of the policy sets |
| policy-sets | `GET /tenants/{t}/policy-sets/{object_id}/rendered` | viewer | Rendered config of a policy set |
| change-sets | `GET /tenants/{t}/change-sets` | viewer | List change sets |
| change-sets | `POST /tenants/{t}/change-sets` | operator | Open a draft change set |
| change-sets | `GET /tenants/{t}/change-sets/{cs}` | viewer | Get a change set with its operations |
| change-sets | `POST /tenants/{t}/change-sets/{cs}/apply` | operator | Apply a change set |
| change-sets | `POST /tenants/{t}/change-sets/{cs}/approve` | tenant-admin | Approve and apply a change set |
| change-sets | `GET /tenants/{t}/change-sets/{cs}/diff` | viewer | Preview what a change set would change |
| change-sets | `POST /tenants/{t}/change-sets/{cs}/discard` | operator | Discard a draft |
| change-sets | `POST /tenants/{t}/change-sets/{cs}/ops` | operator | Add operations to a draft |
| change-sets | `DELETE /tenants/{t}/change-sets/{cs}/ops/{index}` | operator | Remove an operation from a draft |
| change-sets | `POST /tenants/{t}/change-sets/{cs}/reject` | tenant-admin | Reject a change set |
| change-sets | `POST /tenants/{t}/change-sets/{cs}/request-approval` | operator | Ask for approval |
| revisions | `GET /tenants/{t}/revisions` | viewer | List revisions |
| revisions | `GET /tenants/{t}/revisions/{n}` | viewer | Get a revision with its full desired state |
| revisions | `POST /tenants/{t}/revisions/{n}/rollback` | tenant-admin | Open a change set that restores a revision |
| engines | `POST /enroll` | public | Trade an enrollment token and CSR for a certificate |
| engines | `GET /tenants/{t}/engines` | viewer | List every engine of the tenant |
| engines | `DELETE /tenants/{t}/engines/{engine}` | tenant-admin | Remove an engine record |
| engines | `GET /tenants/{t}/engines/{engine}` | viewer | Get one engine |
| engines | `POST /tenants/{t}/engines/{engine}/enrollment-token` | tenant-admin | Create an agent enrollment token |
| engines | `PUT /tenants/{t}/engines/{engine}/state` | tenant-admin | Enable or disable an engine |
| engines | `GET /tenants/{t}/se-groups/{group}/engines` | viewer | List an SE group's engines |
| engines | `POST /tenants/{t}/se-groups/{group}/engines` | tenant-admin | Register an engine |
| infrastructure | `POST /tenants/{t}/engines/{engine}/retire` | tenant-admin | Retire an engine and destroy its VM |
| infrastructure | `POST /tenants/{t}/networks/apply-sdn` | tenant-admin | Apply the Proxmox SDN objects of managed networks |
| infrastructure | `POST /tenants/{t}/se-groups/{group}/deploy` | tenant-admin | Deploy engines for an SE group on Proxmox |
| status | `GET /tenants/{t}/status/engines` | viewer | Last observed state of every engine |
| status | `GET /tenants/{t}/status/stream` | viewer | Live events (server-sent events) |
| status | `GET /tenants/{t}/status/virtual-services` | viewer | State of every virtual service |
| status | `GET /tenants/{t}/summary` | viewer | Tenant home summary |
| search | `GET /tenants/{t}/search` | viewer | Find objects by name |
| jobs | `GET /jobs/{job_id}` | authenticated | Get a background job |
| jobs | `GET /tenants/{t}/jobs` | viewer | List a tenant's jobs, newest first |
| jobs | `GET /tenants/{t}/jobs/{job_id}` | viewer | Get a job with its stages |
| jobs | `POST /tenants/{t}/jobs/{job_id}/cancel` | operator | Cancel a job that has not started |
| jobs | `GET /tenants/{t}/jobs/{job_id}/log` | viewer | Get a job's log |
| jobs | `POST /tenants/{t}/jobs/{job_id}/retry` | operator | Queue a dead job again |
| audit | `GET /audit-actions` | authenticated | Catalog of audit event actions |
| audit | `GET /audit-events` | system-admin | List audit events across tenants (system-admin) |
| audit | `GET /tenants/{t}/audit-events` | viewer | List audit events, newest first |
| tokens | `GET /system/tokens` | system-admin | List system API tokens |
| tokens | `POST /system/tokens` | system-admin | Create a system API token (shown once) |
| tokens | `DELETE /system/tokens/{token}` | system-admin | Revoke a system API token |
| tokens | `GET /tenants/{t}/tokens` | tenant-admin | List API tokens |
| tokens | `POST /tenants/{t}/tokens` | tenant-admin | Create an API token (shown once) |
| tokens | `DELETE /tenants/{t}/tokens/{token}` | tenant-admin | Revoke a token |
| access | `GET /role-mappings` | system-admin | List role mappings (system-wide) |
| access | `POST /role-mappings` | system-admin | Create a role mapping (system-wide) |
| access | `DELETE /role-mappings/{mapping}` | system-admin | Delete a role mapping |
| access | `GET /role-mappings/{mapping}` | system-admin | Get a role mapping |
| access | `PATCH /role-mappings/{mapping}` | system-admin | Change a role mapping |
| access | `GET /tenants/{t}/role-mappings` | tenant-admin | List a tenant's role mappings |
| access | `POST /tenants/{t}/role-mappings` | tenant-admin | Create a role mapping in a tenant |
| access | `DELETE /tenants/{t}/role-mappings/{mapping}` | tenant-admin | Delete a role mapping in a tenant |
| access | `PATCH /tenants/{t}/role-mappings/{mapping}` | tenant-admin | Change a role mapping in a tenant |
| access | `GET /tenants/{t}/users` | tenant-admin | List people with access to a tenant |
| access | `GET /users` | system-admin | List users |
| access | `DELETE /users/{user}` | system-admin | Delete a user |
| access | `GET /users/{user}` | system-admin | Get a user |
| access | `POST /users/{user}/disable` | system-admin | Disable a user |
| access | `POST /users/{user}/enable` | system-admin | Enable a user |
| access | `POST /users/{user}/revoke-sessions` | system-admin | End a user's sessions |
| identity providers | `GET /identity-providers` | system-admin | List identity providers |
| identity providers | `POST /identity-providers` | system-admin | Create an identity provider |
| identity providers | `DELETE /identity-providers/{idp}` | system-admin | Delete an identity provider |
| identity providers | `GET /identity-providers/{idp}` | system-admin | Get an identity provider |
| identity providers | `PATCH /identity-providers/{idp}` | system-admin | Change an identity provider |
| identity providers | `POST /identity-providers/{idp}/test` | system-admin | Start a test sign-in |
| identity providers | `GET /identity-providers/{idp}/test/{test_id}` | system-admin | Read the result of a test sign-in |
| break-glass | `GET /system/break-glass` | system-admin | Break-glass login status |
| break-glass | `PUT /system/break-glass` | system-admin | Switch break-glass login on or off |
| break-glass | `DELETE /system/break-glass/admin` | system-admin | Remove the break-glass administrator |
| break-glass | `POST /system/break-glass/admin` | system-admin | Enrol the break-glass administrator |
| break-glass | `POST /system/break-glass/admin/confirm` | system-admin | Confirm the authenticator |

**Not backed by an API yet:** host online state of
Proxmox nodes, a telemetry pipeline behind metrics and logs, label filters (dropped).

---

## 5. Object shapes (all in OpenAPI; summarised here)

**Read envelope (all objects):** every object returns its create fields plus `id` (uuid), `version` (int; also the ETag), `tenant_id`, `draft_status` (`unchanged|modified|created|deleted`), `changed_fields` (string[]).

**Policy set (`kind` discriminator):**
- `redirect`: `name`, `rules[]` each `{match{host, path_prefix, path_exact}, redirect{location, code 301|302|303|307|308}}` (≥1 rule).
- `allow_list`: `name`, `cidrs[]` (≥1), `managed_by: "cloudflare" | null`.

**Routing profile (`kind` discriminator):**
- `vrrp`: `name`, `vrid` 1–255, `interface`, `advert_interval_s` 1–10, `base_priority` 2–254, `priority_step` 1–50, `static_routes[]` `{prefix, next_hop}`.
- `bgp`: `name`, `asn`, `bfd` (bool), `peers[]` `{peer_ip, remote_asn, …}` (≥1; MD5 is a `secret_ref`, never a value), `max_paths` 1–64, `static_routes[]`.

**Engine status (`status/engines`, event `status`):** `engine`, `engine_id`, `se_group_id`, `reachable`, `error`, `haproxy_fingerprint`, `members[{backend, server, status}]`, `agent{version, bundle_id, haproxy_healthy, routes_withdrawn, bgp[{peer, state, remote_asn}], vrrp[{name, state}], ipvs[…], file_hashes}`, `drift[{kind: haproxy_config|agent_bundle|agent_file|agent_unexpected_file, expected, actual, path}]`, `observed_at`. Polled about every 10 s.

**Virtual service status (`status/virtual-services`, event `vs_state`):** `id`, `name`, `protocol`, `state` (`serving|disabled|not_served`), `health` (`healthy|degraded|down|unknown|disabled`), `members_up`, `members_total`.

**Change-set diff:** `{objects[{type,id,name,status,changed_fields}], problems[{path,code,message}], warnings[{code,path,message}], impact{services[{id,name,vip,se_group_id}], vips[], se_groups[{id,name}], hostnames[]}, se_groups[{id,name,error,files[{path,status,diff,before,after}]}]}`.

**Stream events (`status/stream`):** `status` (array of engine status), `vs_state` (array), `job_progress` (one job), `revision_applied` (one revision summary); every event has an `id`.

---

## 6. Handoff checklist for Claude Design

1. **Design system first** (per 03 §"Design handoff"): shadcn tokens + Switchyard status tokens + custom components. This doc assumes it exists; nothing here redefines it. The Claude Design canvas is the visual source.
2. **Give Claude Design:** this file, `docs/api.md`, `docs/api/openapi.json` (or the live `/api/v1/docs` and `/api/v1/redoc`), `docs/design/05-frontend-backend-tickets.md` (G1 spec for the access screens), and ADRs 0007, 0012, 0016, 0030, 0040.
3. **Order of screens (U1 first):** app shell + change-set bar → tenant switcher → VS list/detail/wizard → pools, monitors, certificates, policies → review (Objects/Config/Impact), apply progress, approval, conflicts → revisions and rollback. U2: live status, SE groups, routing profiles, jobs, tenant home, metrics. U3: networks/IPAM, SE deploy. U4: certificate issuance, admin and access.
4. **For every screen deliver:** empty, loading, error, open draft, pending approval, read-only and (where status exists) drift/stale states; 1280 px and 1920 px; light and dark.
5. **Mark "blocked on API"** only for the ❌ items: host online state; and show the "no data source" state for metrics and logs.

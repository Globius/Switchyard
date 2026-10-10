# Switchyard — Frontend Scope and Claude Design Handoff

Date: 2026-10-10. Companion to [03-frontend-design.md](03-frontend-design.md) (stack, UX principles, design system) and [02-backend-design.md](02-backend-design.md).

This doc does two things: (1) audits the implemented controller API against what the UI needs, and (2) gives Claude Design everything per screen: entities and fields, states, interactions and endpoints.

**Sources read.** The implemented API is M1–M3 in the worktree `.claude/worktrees/compassionate-boyd-621fa7` (`controller/switchyard/api/app.py`, `docs/api/openapi.json`, ADRs 0006–0008, `domain/models.py`). The main checkout has docs only. M4 is being built in parallel, so U3 screens (networks, SE deploy) are scoped against the design, not against code.

Legend: ✅ implemented and in OpenAPI · ⚠️ implemented but under-documented or partial · ❌ not implemented.

---

## 1. Verdict

**The API is not yet ready to hand the whole UI to Claude Design as "build against this."** Design can start now (screens, states, flows are all defined below), but code generation would hit these blockers, in priority order:

| # | Gap | Impact | Needed before |
| --- | --- | --- | --- |
| G1 | **No user login.** Only `Authorization: Bearer <api token>`. No OIDC/SAML, no session cookie, no CSRF, no `GET /me`, no identity-provider list. ADR 0007 defers it "to a later step"; the plan has it in U4/M5. | Login screen, `can(verb, type)` permission hook, "who am I" in the top bar and per-tenant roles cannot be built against a real API. | U1 |
| G2 | **Response bodies are untyped.** OpenAPI has request models (`*Create`, `*Patch`) but every response is `object` or `Page{items: object[]}`. `openapi-typescript` would emit `unknown`. | Typed client and `orval` Zod schemas give nothing for reads. Fix: add read models (`VirtualServiceRead` = create fields + `id`, `version`, `tenant_id`, `draft_status`, `changed_fields`) and typed responses for change sets, diff, revisions, jobs, status, audit, tokens, engines. | U1 |
| G3 | **No "Rendered config" per object.** The only rendered output is `GET /change-sets/{cs}/diff` → `se_groups[].files`, and its file shape is undocumented. Design has a Rendered config tab on every object page. | Needs `GET …/{object}/{id}/rendered` (live and `?change_set=`), or an agreed rule that the tab shows the SE-group file filtered to that object. | U1 |
| G4 | **No metrics or logs.** RPS, p95, 5xx, latency breakdown, log tail, sparklines have no endpoint (the `metrics` adapter and Grafana proxy are not built). | VS list columns and VS detail charts have no data source. | U2 |
| G5 | **Live stream is one coarse event.** `GET /status/stream` emits a single `event: status` carrying the full engine list (polled from DB every 2 s, only when changed). The design's event catalog (`member_status`, `vs_health`, `bgp_session`, `job_progress`, `revision_applied`) does not exist. `members`, `agent`, `drift` are untyped dicts. | Cache patching needs either typed `status` payloads (workable for U2) plus job polling, or the finer events. | U2 |
| G6 | **Jobs are fetch-by-id only.** No list, logs, stage progress, retry or cancel. Apply progress (lock → render → validate → canary → rollout → commit) is not exposed; `result` is free-form. | Jobs page and apply progress screen need a typed job model with stages, plus `GET /jobs`, `POST /jobs/{id}/retry|cancel`. Jobs also aren't tenant-scoped in the path. | U2 |
| G7 | **Policy sets and routing profiles have free-form bodies** (`additionalProperties: true`) because they are discriminated unions. No schema reaches the generated client. | Rule builder and BGP/VRRP forms can't be generated. Fix: publish `oneOf` with `kind` discriminator (`redirect`, `allow_list` / `vrrp`, `bgp`). Shapes are in §5 so design isn't blocked. | U1 |
| G8 | **Change-set conflicts and impact.** No three-way conflict endpoint or documented 409/412 body; `ops[].expected_version` exists but the response shape on stale version is not specified. The diff has `problems` but no "Impact" section (VIPs, SEs, hostnames touched). | ConflictResolver and the Impact tab need server data (or client derivation from `objects` + snapshot). | U1 |
| G9 | **Missing endpoints for later screens:** cert renew/issue (M5), SE group deploy and VM placement (M4), VIP allocations and release, health-monitor test, identity providers and role mappings, audit filters/cursor, tenant "require approval" history. Details in §4. | Blocks U3/U4 only. Not blocking design. | U3–U4 |
| G10 | **Minor:** `?label=` and `?se_group=` filters from the design are not implemented (only `name`, `change_set`, `cursor`, `limit`); list sort is fixed to name; `GET /revisions` and `GET /change-sets` are not paginated; `GET /change-sets` omits `base_revision`, `created_at`, op count; no OpenAPI descriptions or examples (summaries are auto-generated, e.g. "Create Pools"); no ETag documented on reads. | Tables, saved filters and MSW mocks (which the plan builds "from OpenAPI examples") are weaker. | U1 |

**What works today and is enough for design:** CRUD for all nine object types via change sets, draft reads (`?change_set=` with `draft_status` and `changed_fields`), change-set lifecycle including four-eyes approval, diff (objects, validator problems, rendered files per SE group), revisions with snapshot and rollback, runtime member drain/enable/disable, engine register/enable/remove, tokens, audit events, engine status snapshot and SSE, RFC 9457 errors with field paths, cursor pagination, ETag/If-Match.

**Recommended backend asks** (one ticket each): G1 session auth + `/me`; G2 read models; G3 rendered endpoint; G7 union schemas; G6 job model. G4/G5 can wait for U2. These should be raised with the M4 owner so they don't collide with controller work in flight.

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

**Async.** Anything that returns `202` carries a `job_id`; show `JobProgress` and poll `/jobs/{id}` (until G6 is closed).

**Optimistic locking.** Object PATCH sends `If-Match: "<version>"`; change-set ops carry `expected_version`. Stale → conflict flow (§3.13).

---

## 3. Screens

Each screen lists entities and fields (names are API field names), states beyond the standard ones, key interactions, and endpoints. All paths are under `/api/v1`. `{t}` = tenant.

### 3.1 App shell
- **Regions:** top bar (tenant switcher, command palette trigger, user menu, theme toggle, SSE connection indicator), left section nav, bottom change-set bar (only with an open draft), toast area.
- **Change-set bar fields:** `summary` (editable name), `author`, `state`, op count, per-type counts, buttons Review & apply / Discard. States: draft, pending_approval ("Awaiting approver"), applying (progress), failed (error + "Open review").
- **Command palette:** jump to any object by name. Needs a name search over all collections (today `?name=` per collection; fan out client-side).
- **Endpoints:** `GET /tenants` ✅, `GET /tenants/{t}/change-sets?state=draft` ✅ (list is thin ⚠️ G10), `POST /tenants/{t}/change-sets` ✅, `GET /me` ❌ G1.

### 3.2 Login
- **Shows:** enabled identity providers (name, protocol oidc|saml), optional break-glass link (password + TOTP).
- **States:** loading, no providers configured, provider error, session expired (returns to prior URL).
- **Endpoints:** provider list ❌, OIDC/SAML redirect and callback ❌, break-glass ❌ (all G1). Temporary stand-in: "paste an API token" form (works today with bearer auth).

### 3.3 Tenant home
- **Cards:** health summary (VSs by status), failing members (from status `members`), drifted engines (`drift`), expiring certs (`certificates[].not_after`, warn at 30 days), recent revisions, running jobs, open drafts.
- **Interactions:** each card item links to the object; "Jump to problems".
- **Endpoints:** `GET status/engines` ✅⚠️ (untyped dicts), `GET certificates` ✅, `GET revisions` ✅, `GET change-sets` ✅, jobs list ❌ (G6), VS health rollup ❌ (derive from status; VS-level health not exposed).

### 3.4 Virtual services list
- **Columns (API):** `name`, `vip`, `ports[].port`, `protocol`, `se_group_id` (resolve to name), `enabled`, `draft_status`. **Columns without data today:** health, RPS, p95, 5xx (G4), cert expiry (derive from `certificate_ids` → `not_after`).
- **Interactions:** filter by name (✅ `?name=`), by SE group/label (❌ G10), sort (fixed by name today), enable/disable (PATCH `enabled` → draft op), create (wizard), delete.
- **States:** standard; plus "N unsaved changes" in draft mode and deleted-in-draft rows.
- **Endpoints:** `GET virtual-services?name&change_set&cursor&limit` ✅, `PATCH virtual-services/{id}` ✅ (needs `If-Match`), `DELETE` ✅.

### 3.5 Virtual service detail (tabs: Overview, Settings, Rendered config, History)
- **Settings fields:** `name`, `enabled`, `protocol` (http|tcp|udp), `se_group_id`, `vip` or `vip_pool_id` (allocate), `ports[]` (`port`, `tls`, `http2`, `proxy_protocol`, `https_redirect_port`), `certificate_ids[]`, `default_pool_id`, `routes[]` (`order`, `match{host, path_prefix, path_exact}`, `pool_id` | `redirect{location, code}`), `policy_set_ids[]`, `allow_list_id`, `enforce_allow_list`, `restore_client_ip_header`.
- **Overview:** route table (host/path → pool), live member status per pool (from status `members`), latency breakdown and log tail (❌ G4), rendered `frontend` snippet.
- **Interactions:** edit fields (adds ops), add/reorder routes, drain a member (runtime, `Live`), enable/disable.
- **Validation messages to design:** VIP outside the SE group's networks, port collision on a VIP, cert SANs not covering a route host, route with both pool and redirect, route with neither (field paths come back in `errors[].path`).
- **Endpoints:** `GET/PATCH virtual-services/{id}?change_set=` ✅, `PUT pools/{pool}/members/{member}/state` ✅, rendered ❌ G3, History tab: audit events filtered by object ❌ (audit has only `limit`).

### 3.6 Create virtual service wizard
Steps: basics (`name`, `protocol`, `se_group_id`) → VIP (`vip_pool_id` pick or fixed `vip`) → ports and TLS (`ports[]`, `certificate_ids[]`) → routes and pools (`routes[]`, `default_pool_id`, create pool inline) → health check (pick/create monitor) → review (field summary + validator problems) → "Add to change set".
- **States:** per-step validation, back/next retains input, no VIP pools available (empty with link to Networks and VIP pools), no certs for TLS (link to import).
- **Endpoints:** `POST virtual-services?change_set=` ✅ (returns `Accepted{change_set_id, object_id, op_index}`); inline pool/monitor creation uses additional ops via `POST change-sets/{cs}/ops` ✅ (atomic multi-object). Pre-flight validation without saving: use the diff `problems` ✅.

### 3.7 Pools
- **Fields:** `name`, `algorithm` (roundrobin|leastconn|source|random), `health_monitor_id`, `tls`, `tls_verify`, `persistence` (`type`, `table_size`, `timeout_s`; source-IP only today), `members[]` (`id`, `name`, `address`, `port`, `weight`, `admin_state` enabled|disabled|drain).
- **Live per member:** state, last check result (from status `members`, shape undocumented ⚠️).
- **Interactions:** add/remove/edit members (draft), enable/disable/drain at runtime (`Live`, confirm dialog, response `{state, applied, failed}` lists per-engine results → show partial failure), edit pool.
- **Endpoints:** CRUD ✅; `PUT pools/{pool}/members/{member}/state` ✅.

### 3.8 Health monitors
- **Fields:** `name`, `type` (tcp|http), `interval_s`, `timeout_s`, `rise`, `fall`, `port`, `http{method GET|HEAD|OPTIONS, path, host, expect_status[]}`; "pools using it" (derive client-side from pools).
- **Interactions:** edit; "test against one member" ❌ (G9).
- **Endpoints:** CRUD ✅.

### 3.9 Certificates
- **Fields:** `name`, `source` (upload|cloudflare_origin|acme|step_ca), `sans[]`, `not_after`, `renew_policy` (manual|auto), `secret_ref` (an OpenBao reference; never show or accept key material in the form), "VSes using it" (derive).
- **Expiry presentation:** ≤30 days warning, ≤7 danger, expired down; icon and text with each.
- **Interactions:** import (name, SANs, secret_ref picker, not_after), edit renew policy, delete (blocked when in use; show which VSes). Issue (ACME / Origin CA) and "Renew now" are ❌ until M5 — design them as disabled with a "coming in U4" note.
- **Note:** today certificates are metadata only; engines must already hold the PEMs (ADR 0007). The import dialog should say so until M5.
- **Endpoints:** CRUD ✅; renew/issue ❌ (G9, M5).

### 3.10 Policies
- **Kinds** (shapes in §5): `redirect` (rules: match + redirect), `allow_list` (CIDRs, optional `managed_by: cloudflare`).
- **UI:** list with kind filter; rule builder for redirects; CIDR list editor with validation for allow lists; Cloudflare-managed lists are read-only with a "Managed by Cloudflare sync" badge.
- **Endpoints:** CRUD ✅⚠️ (free-form body, G7).

### 3.11 Networks and VIP pools
- **Network fields:** `name`, `cidr`, `gateway`, `sdn_zone`, `vnet`, `vlan_or_vni`, `managed` (created by controller vs attached).
- **VIP pool fields:** `name`, `network_id`, `ranges[]` (`start`, `end`).
- **Needed for design but not in API:** IP usage bar (used/total per pool), allocations list (IP → VS), release IP (❌ G9). Design with these.
- **States:** pending SDN apply (job), network in use (delete blocked).
- **Endpoints:** CRUD ✅ (both); allocations/usage ❌ (domain has `ipam.py`; no route). M4 owns Proxmox/SDN.

### 3.12 SE groups, engines, routing profiles
- **SE group fields:** `name`, `ha_mode` (bgp|vrrp), `networks[]` (`network_id`, `role` vip|backend|mgmt), `maxconn`, `canary_seconds`, `routing_profile_id`.
- **Engines (SEs) per group:** `id`, `name`, `url` (data plane), `state` (enabled|disabled); register via `EngineIn` (`name`, `dataplane_url`, `credentials_ref`, `mgmt_ip`, `agent_url`). Live per engine: `reachable`, `error`, `haproxy_fingerprint` (config hash), `agent` (VRRP/BGP state, untyped), `drift`, `observed_at`.
- **Interactions:** register, enable/disable, remove engine (runtime, `Live`); deploy VM, add/remove VM, redeploy one SE (❌ M4, show as the U3 target).
- **Routing profiles** (shapes in §5): VRRP profile or BGP profile with peers; BGP peer session state and advertised prefixes (❌ not in API; the `agent` status blob may carry it, undocumented).
- **Endpoints:** SE groups CRUD ✅; engines `GET/POST se-groups/{group}/engines`, `PUT engines/{engine}/state?state=`, `DELETE engines/{engine}` ✅; `GET status/engines` ✅⚠️; routing profiles CRUD ✅⚠️.

### 3.13 Change sets: review, apply, conflicts, approval
Screens: list (open drafts, history), full-screen review (tabs Objects / Config / Impact), apply progress, conflict resolver, approval banner.
- **List fields:** `id`, `summary`, `author`, `state` (draft|pending_approval|applying|applied|failed|discarded). Detail adds `base_revision`, `approver`, `reject_comment`, `error`, `created_at`, `ops[]` (`action` create|update|delete, `object_type`, `object_id`, `body`, `expected_version`).
- **Review / Objects:** from `diff.objects[]` (`type`, `id`, `name`, `status`, `changed_fields`) with before/after per field (client computes from live vs draft reads, or needs the diff to include values ⚠️). **Config:** `diff.se_groups[]` (`id`, `name`, `error`, `files[]`). **Impact:** `diff.problems[]` (`path`, `code`, `message`) shown as warnings/blocks; the VIPs/SEs/hostnames list needs G8.
- **Apply:** `POST …/apply` → `202 {job_id}`; optional body `{break_glass_reason}` for system-admin bypass (mandatory reason field in the dialog, flagged in audit). Progress stages per the design; failure shows the failed stage and `error`; draft stays open.
- **Approval (when tenant `require_approval`):** button becomes "Request approval" → state `pending_approval`; approver (different person) sees the same review with Approve (applies) / Reject (required `comment`, reopens draft). Show approver/reject_comment in the banner. Edits after request void it.
- **Conflicts:** stale `expected_version` → three-way view (base, theirs, yours) per field (needs G8).
- **Endpoints:** all ✅ except conflict and progress detail: `GET/POST change-sets`, `GET change-sets/{cs}`, `POST …/ops`, `DELETE …/ops/{index}`, `GET …/diff`, `POST …/apply|discard|request-approval|approve|reject`.

### 3.14 Revisions
- **Fields:** `number`, `author`, `summary`, `git_sha`, `created_at`; detail adds `snapshot` (full desired state).
- **Interactions:** diff any two (client-side from two snapshots ✅ possible; confirm a diff view over object JSON), "Roll back to #N" → `POST revisions/{n}/rollback` returns a pre-filled change set (`201`, same shape as change-set detail) and opens the standard review.
- **Endpoints:** `GET revisions` ✅, `GET revisions/{n}` ✅, `POST revisions/{n}/rollback` ✅.

### 3.15 Jobs
- **Fields available:** `id`, `kind`, `state`, `attempts`, `error`, `result`. **Needed:** list, started/finished times, stages, log lines, linked change set, retry, cancel (❌ G6).
- **Note:** a reverted or rejected apply is a *finished* job with `result.ok: false`, not a failed one (ADR 0007). Design must show "Reverted" distinct from "Retrying after infrastructure error".
- **Endpoints:** `GET /jobs/{id}` ✅ (no tenant in path).

### 3.16 Admin (system-admin only)
- **Tenants:** `name`, `oidc_group`, `quotas` (map of name → int), `require_approval` toggle (audited; settings page shows who/when from audit). `GET/POST /tenants`, `GET/PATCH/DELETE /tenants/{t}` ✅.
- **API tokens:** list `id`, `name`, `role` (viewer|operator|tenant-admin|system-admin), `revoked`, `expires_at`; create (`name`, `role`, `ttl_days`) returns the plaintext `token` **once** — design a copy-once dialog. Revoke is runtime + confirm. ✅.
- **Audit log:** `at`, `actor`, `action`, `object`, `diff`, `request_id`. Only `limit` (≤500) supported; filter by actor/action/object/date and cursor ❌ G10.
- **Identity providers, role mappings:** ❌ G1/G9 (fields in backend design: name, protocol, issuer/metadata URL, client_secret_ref, claim mapping, enabled; group → role per tenant).

---

## 4. Endpoint inventory (implemented)

Base `/api/v1`. All except `healthz` need `Authorization: Bearer <token>`. Errors are `application/problem+json` (`type`, `title`, `status`, `detail`, `errors[{path, code, message}]`).

| Area | Endpoints |
| --- | --- |
| Meta | `GET /healthz` |
| Tenants | `GET/POST /tenants`; `GET/PATCH/DELETE /tenants/{t}` |
| Objects (×9: `networks`, `vip-pools`, `se-groups`, `routing-profiles`, `virtual-services`, `pools`, `health-monitors`, `certificates`, `policy-sets`) | `GET /tenants/{t}/{type}?name&change_set&cursor&limit`; `GET …/{id}?change_set`; `POST …?change_set=|apply=true` (202 `Accepted`); `PATCH …/{id}` (needs `If-Match`); `DELETE …/{id}` |
| Change sets | `GET/POST /change-sets`; `GET /{cs}`; `POST /{cs}/ops`; `DELETE /{cs}/ops/{index}`; `GET /{cs}/diff`; `POST /{cs}/apply`, `/discard`, `/request-approval`, `/approve`, `/reject` |
| Revisions | `GET /revisions`; `GET /revisions/{n}`; `POST /revisions/{n}/rollback` |
| Runtime | `PUT /pools/{pool}/members/{member}/state`; `PUT /engines/{engine}/state?state=`; `DELETE /engines/{engine}`; `GET/POST /se-groups/{group}/engines` |
| Status | `GET /status/engines`; `GET /status/stream?follow&interval` (SSE, `event: status`) |
| Tokens / audit / jobs | `GET/POST /tokens`, `DELETE /tokens/{token}`; `GET /audit-events?limit`; `GET /jobs/{job_id}` |

**Still missing vs design (ordered by milestone):** `/me` and login (G1); rendered config (G3); metrics and logs (G4); finer SSE events and jobs list/stages (G5, G6); VIP allocations and release, health-monitor test (U2/U3); SE group deploy and Proxmox placement (M4); cert issue/renew (M5); identity providers and role mappings (M5/U4); audit filters; tenant-level `GET /search`.

---

## 5. Object shapes Claude Design needs (not in OpenAPI yet)

**Read envelope (all objects):** every object returns its create fields plus `id` (uuid), `version` (int; also the ETag), `tenant_id`, `draft_status` (`unchanged|modified|created|deleted`), `changed_fields` (string[]).

**Policy set (`kind` discriminator):**
- `redirect`: `name`, `rules[]` each `{match{host, path_prefix, path_exact}, redirect{location, code 301|302|303|307|308}}` (≥1 rule).
- `allow_list`: `name`, `cidrs[]` (≥1), `managed_by: "cloudflare" | null`.

**Routing profile (`kind` discriminator):**
- `vrrp`: `name`, `vrid` 1–255, `interface`, `advert_interval_s` 1–10, `base_priority` 2–254, `priority_step` 1–50, `static_routes[]` `{prefix, next_hop}`.
- `bgp`: `name`, `asn`, `bfd` (bool), `peers[]` `{peer_ip, remote_asn, …}` (≥1; MD5 is a `secret_ref`, never a value), `max_paths` 1–64, `static_routes[]`.

**Engine status (`status/engines`, SSE `status`):** `engine`, `engine_id`, `se_group_id`, `reachable`, `error`, `haproxy_fingerprint`, `members` (dict, undocumented), `agent` (dict, undocumented: VRRP/BGP/HAProxy health), `drift` (undocumented), `observed_at` (ISO). Poll interval ≈10 s; stream compares and emits on change.

**Change-set diff:** `{objects[{type,id,name,status,changed_fields}], problems[{path,code,message}], se_groups[{id,name,error,files[…]}]}`.

---

## 6. Handoff checklist for Claude Design

1. **Design system first** (per 03 §"Design handoff"): shadcn tokens + Switchyard status tokens + custom components. This doc assumes it exists; nothing here redefines it.
2. **Order of screens (U1 first):** app shell + change-set bar → tenant switcher → VS list/detail/wizard → pools, monitors, certificates, policies → review (Objects/Config/Impact), apply progress, approval, conflicts → revisions and rollback. U2: live status, SE groups, routing profiles, jobs, tenant home. U3: networks/IPAM, SE deploy. U4: certificate issuance, admin.
3. **For every screen deliver:** empty, loading, error, open draft, pending approval, read-only and (where status exists) drift/stale states; 1280 px and 1920 px; light and dark.
4. **Design with data that doesn't exist yet** (G4 metrics, IP usage, job stages, three-way diff) but mark those regions "blocked on API" in the mockups so the build isn't surprised.
5. **Open the backend tickets for G1, G2, G3, G7, G6** now; U1 code generation depends on G1–G3 and G7.

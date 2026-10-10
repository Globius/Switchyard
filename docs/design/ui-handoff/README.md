# Switchyard UI — design handoff

Date: 2026-10-10. From Claude Design to the build (milestones U1–U4).

The designs live on the **Switchyard UI** canvas (a private Claude Design artifact owned by Miguel; ask him to share it).
This folder is the portable copy: theme, screen map, behavior rules, the design source files and screenshots.

Backend sources of truth: `docs/api/openapi.json`, `docs/api.md`, `docs/design/04-frontend-scope.md`,
`docs/design/05-frontend-backend-tickets.md`, ADRs 0007, 0012, 0016, 0030–0032, 0040, 0041.
If this handoff and the API disagree, the API wins; tell design.

## What's in this folder

| Path | What it is |
| --- | --- |
| `README.md` | This file: screen map, behaviors, components, checks, open items |
| `globals.css` | Proposed `ui/src/globals.css`: shadcn/ui theme (Tailwind v4, OKLCH) plus Switchyard status tokens, light and dark |
| `design-source/*.dc.html` | One file per canvas artboard (HTML mockups). Reference only: copy wording, layout and states from them, not code |
| `design-source/sy.css` | The mockups' shared stylesheet. Same tokens as `globals.css`; the component classes are mock-only |
| `design-source/canvas.json` | Canvas layout (which artboard sits in which row). The `.dc.html` files need the canvas runtime (`support.js`) to render their loops and states; read them as markup, or use the screenshots |
| `screenshots/<Screen>-1280.png`, `-1920.png` | Each screen in its default theme at both checked widths |
| `screenshots/alt-theme/<Screen>-1280.png` | Each screen in the other theme |

Sample data (tenant `globius-prod`, people, IPs, names) is invented and consistent across screens. None of it is real.

## Foundations

- **Stack** as in 03: React 19, TypeScript strict, Vite, TanStack Router/Query/Table, shadcn/ui (Radix + Tailwind v4),
  React Hook Form + Zod from OpenAPI, Monaco (read-only diff and rendered config), `cmdk`.
- **Theme:** commit `globals.css` as is. No `ui/globals.css` existed, so this is the proposal the canvas was drawn with.
- **Type:** Geist (UI) and Geist Mono (IPs, ports, IDs, config). Sizes: page title 24/600, section 18/600 (cards 15/600),
  body 14, tables 13, captions 12, mono 13 (12 in dense places).
- **Spacing and radius:** 4 px grid (4, 8, 12, 16, 24, 32, 48). Radius 8 cards, 6 inputs and buttons, full for pills.
- **Layout:** top bar 56 px; left nav ~220 px (stacks on narrow screens); content padding 24–32 px; change-set bar fixed
  at the bottom whenever a draft is open. Designed at 1440, checked at 1280 and 1920.
- **Colour rule:** every status shows an icon plus a text label. Never colour alone.
- **Status vocabulary** (token → meaning → icon):
  `status-healthy` (up, healthy, in sync, succeeded) check-circle · `status-degraded` (degraded, drifted, expiring,
  warning, retrying) triangle · `status-down` (down, failed, dead, reverted, expired) x-circle · `status-draining`
  arrow-down-to-line · `draft` (anything differing from live) pencil · `live` (runtime action, outside the change set)
  zap · `pending-approval` (awaiting approval, running) clock. Neutral grey pill for disabled, not applied, unknown.

## Navigation

```
Top bar: logo · TenantSwitcher · command palette (Ctrl/Cmd K) · stream indicator · theme · user menu
Tenant nav (/t/:tenant/…)
  Home
  Traffic: Virtual services · Pools · Health monitors · Certificates · Policies
  Infrastructure: Networks and VIP pools · SE groups · Routing profiles
  Changes: Change sets · Revisions · Jobs
  Tenant: Access and settings
  System: Admin                      (system-admins only)
Admin nav (/admin/…): Tenants · Users · Identity providers · Role mappings · System tokens · Audit log
Outside the shell: Login, Review and apply (full screen), Create wizard (full screen)
```

## Screen map

Artboard = file in `design-source/` and title on the canvas. Endpoints are under `/api/v1`; `{t}` is the tenant.

| Screen | Artboard | Main endpoints | Milestone |
| --- | --- | --- | --- |
| Foundations and components | `Main` | — | U1 |
| Tenant switcher, command palette, user menu | `Overlays` | `GET /tenants`, `GET /tenants/{t}/search`, `GET /me`, `POST /auth/refresh`, `POST /auth/logout` | U1 |
| Login (4 error states, break-glass) | `Login` | `GET /auth/providers`, `GET /auth/login/{provider}`, `POST /auth/break-glass`; errors arrive as `/login?error=provider_error\|expired\|no_access\|disabled` | U1 |
| Tenant home | `TenantHome` | `GET /tenants/{t}/summary` | U2 |
| Virtual services list | `VirtualServices` | `GET …/virtual-services?q&sort&se_group&protocol&enabled&change_set`, `GET …/status/virtual-services`, `GET …/metrics/virtual-services` | U1 |
| VS Overview | `VirtualServiceDetail` | `GET …/virtual-services/{id}?change_set`, status endpoints, `PUT …/pools/{pool}/members/{member}/state`, `…/metrics`, `…/logs` | U1 (live parts U2) |
| VS Settings | `VirtualServiceSettings` | `PATCH …/virtual-services/{id}` (If-Match) or change-set ops, `GET /schema/virtual-service?protocol=` | U1 |
| VS Rendered config | `VirtualServiceRendered` | `GET …/virtual-services/{id}/rendered?change_set` | U1 |
| VS History | `VirtualServiceHistory` | `GET …/audit-events?object=`, `GET …/revisions?object=` | U1 |
| Create wizard (HTTP / TCP / UDP) | `CreateWizard` | `POST …/change-sets/{cs}/ops` (several ops, atomic), `GET /schema/virtual-service?protocol=`, `POST …/vip-pools/{id}/next-free`, diff `problems` for pre-flight | U1 |
| Review and apply (Objects / Config / Impact, apply, approval, conflict) | `Review` | `GET …/change-sets/{cs}`, `…/diff`, `POST …/request-approval\|approve\|reject\|apply\|discard`, `GET …/jobs/{id}` | U1 |
| Change sets | `ChangeSets` | `GET …/change-sets`, `GET …/change-sets/{cs}` | U1 |
| Revisions (compare two, roll back) | `Revisions` | `GET …/revisions`, `GET …/revisions/{n}`, `POST …/revisions/{n}/rollback` | U1 |
| Jobs | `Jobs` | `GET …/jobs?state&kind`, `GET …/jobs/{id}`, `…/log`, `POST …/retry\|cancel` | U2 |
| Pools (edit sheet) | `Pools` | pools CRUD | U1 |
| Health monitors (edit, test) | `HealthMonitors` | monitors CRUD, `POST …/health-monitors/{id}/test` | U1 (test U3) |
| Certificates (import, issue, renew) | `Certificates` | certificates CRUD, `POST …/certificates/import\|issue`, `POST …/certificates/{id}/renew` | U1 (issue/renew U4) |
| Policies (redirect rules, allow lists) | `Policies` | policy-sets CRUD (`kind` union) | U1 |
| Networks, VIP pools, addresses | `Networks` | networks and vip-pools CRUD, `GET …/vip-pools/{id}/allocations`, `POST …/networks/apply-sdn` | U3 |
| SE groups (engines, placement, deploy, enrollment) | `SeGroups` | se-groups CRUD, `GET …/engines?se_group`, `GET …/status/engines`, `POST …/se-groups/{id}/deploy`, `POST …/engines/{id}/enrollment-token\|retire`, `PUT …/engines/{id}/state`, `DELETE …/engines/{id}` | U2 (deploy U3) |
| Routing profiles (BGP / VRRP) | `RoutingProfiles` | routing-profiles CRUD (`kind` union) | U2 |
| Tenant access and settings (members, mappings, tokens, approval) | `TenantAccess` | `GET …/users`, `…/role-mappings`, `…/tokens`, `PATCH /tenants/{t}` | U4 |
| Admin: tenants | `AdminTenants` | `GET/POST /tenants`, `PATCH/DELETE /tenants/{t}` | U4 |
| Admin: users | `Users` | `GET /users`, `GET /users/{id}`, `POST …/disable\|enable\|revoke-sessions`, `DELETE /users/{id}` | U4 |
| Admin: identity providers and break-glass | `IdentityProviders` | `…/identity-providers` CRUD and `/test`, `GET /auth/saml/{provider}/metadata`, `/system/break-glass` (+ `/admin`, `/admin/confirm`) | U4 |
| Admin: role mappings | `RoleMappings` | `/role-mappings`, `/tenants/{t}/role-mappings`, `GET /permissions` | U4 |
| Admin: system tokens | `AdminTokens` | `/system/tokens` | U4 |
| Admin: audit log | `AuditLog` | `GET /audit-events?tenant&actor&action&object&since&until&q`, `GET /audit-actions` | U4 |
| Standard states | `States` | — | all |

Settings on each artboard (the canvas "Tweaks" panel) switch theme and show other states: wizard step and protocol,
review phase (ready, applying, failed, approval, conflict) and viewer (author, operator, tenant-admin), dialog open or
closed, login error, break-glass state, tenant access tab.

## Behaviors the build must keep

**Change sets**
- Edits never go live on save. The first edit opens a draft; every edit is an op. The change-set bar shows the draft,
  its counts and Review and apply.
- Pages read with `?change_set=<draft>` while a draft is open. Drafted objects and fields get the `draft` dot; deleted
  ones are struck through; created ones say New. Settings show the live value under a changed field, with Revert.
- **Objects tab:** the diff lists changed fields only. Compute before and after from the live read and the draft read.
- **Conflict (412):** the problem carries `conflict` with versions only. Build base / theirs / yours from the base
  revision's snapshot, the current object and the draft, and make the user pick per field.
- **Approval tenants:** Apply returns 403, so never offer it. Show Request approval. Approvers are tenant-admins who are
  neither author nor requester: author sees "You wrote this", operator sees Approve disabled ("needs tenant-admin").
  Any edit withdraws a pending request. Break-glass applies bypass approval with a mandatory reason.
- **Apply progress:** poll the job (or SSE `job_progress`). Stages: lock, render, validate, canary, rollout, commit.
  `succeeded` with `ok: false` is **Reverted** (finished, not retried). `queued` with `retrying: true` is
  **Retrying after error**. `dead` is **Gave up**. Running jobs can't be cancelled; only queued ones.
- **Rollback** opens a pre-filled draft and the normal review.

**Runtime actions** (member drain/enable/disable, engine enable/disable/register/retire/remove, enrollment tokens,
certificate issue/renew, token create/revoke) skip the change set: confirm dialog, Live badge, audited.
Member state responses are per engine; show partial failure. Certificate issue/renew run as jobs that open a
`system:certs` change set (result `change_set_id`), which follows approval like any other.
**Retire** destroys the VM and the record; **Remove** deletes only the record.

**Permissions:** `can(verb, object)` reads `GET /permissions` (enums), never hard-coded rules. Disabled controls stay
visible with a tooltip from the permission's `description` / the 403's `required_role`. Roles nest
(viewer < operator < tenant-admin < system-admin).

**Session:** cookie with `X-CSRF-Token` on writes (value from `/me`). 4 h absolute, 30 min idle. A 401 with
`code: session_expired` opens a re-login modal that keeps form input. Show "Refresh access" (`POST /auth/refresh`);
mapping changes apply within a minute, group membership changes need a new sign-in.

**Live data:** one SSE stream per tab (`status`, `vs_state`, `job_progress`, `revision_applied`) patching TanStack Query
caches. Show `observed_at` on live values and a banner while the stream is down; refetch visible queries on reconnect.

**Drift:** report only (engine, group, service, home). The one offer is an explicit "Re-apply revision #N". Never auto-fix.

**UDP:** forms disable fields per `GET /schema/virtual-service?protocol=udp` (no TLS, HTTP/2, PROXY, routes, policies;
no Random; members need a port). UDP monitors only check UDP pools and have no rise count. Suggest Source hash for WireGuard.

**Secrets:** never displayed. Certificate import takes the PEM once (write-only). Client secrets are write-only ("Set",
Replace). Tokens, enrollment tokens and the break-glass password/TOTP are shown once with Copy and an "I stored it" check.

**VIP addresses:** held by the service that uses them. "Edit service" is the only way to free one.

**Not backed (designed as unavailable):** metrics and logs answer `available: false` with a `reason`; show the
unavailable state with that reason, and "—" (never 0) for missing numbers. Proxmox host online state: "Host state not
reported". Label filters: disabled control, tooltip "Labels aren't supported yet".

**Standard states on every screen** (see `States`): loading skeleton, empty with primary action (text only for
read-only users), error with problem `title`/`detail` and Retry (field errors on the input via `errors[].path`),
read-only (403) with role tooltip, 412 changed by someone else, 401 session expired, stream down / stale, drift,
pending approval (draft frozen), runtime confirm and partial failure, unavailable backend.

## Components

shadcn/ui as is: Button, Input, Textarea, Select, Combobox, Checkbox, Switch, Radio Group, Tabs, Data Table, Dialog,
Alert Dialog, Sheet, Dropdown Menu, Command, Sonner, Badge, Tooltip, Card, Sidebar, Breadcrumb, Form, Alert, Skeleton,
Progress, Toggle Group (segmented controls).

Switchyard custom (`@switchyard` registry): ChangeSetBar, DraftMarker, ReviewDiff (Objects / Config / Impact),
ConflictResolver, StatusPill, LiveBadge, MemberStateList, IpUsageBar, BgpSessionChip, VrrpStateChip, WizardStepper,
ApprovalBanner, JobProgress (stage list), RenderedConfigView (Monaco, range highlight, fingerprint), TenantSwitcher,
plus three that came out of the screens: **UnavailableState** (icon, title, API reason), **ShowOnceSecret** (read-only
field, Copy, "I stored it"), **ChipInput** (SANs, certificates, policy sets, groups).

## Checks done

- Every artboard rendered headless (Chromium) at **1280 and 1920 px** in its default theme, and at 1280 in the other
  theme: no horizontal page overflow, no script errors (only the Google Fonts request fails in the sandbox).
- Fixed during the check: Tenant home cards overflowed at 1280; some screens missed the shared stylesheet; one stale
  certificate sentence in the wizard.
- Wide tables (VS members, jobs, audit) scroll inside their card at 1280; that is intended.
- Accessibility as drawn: real buttons, links, inputs and labels; icon-only buttons have `aria-label`; dialogs have
  `role="dialog"`/`alertdialog`, `aria-modal`, labelled titles; status always has text. Run `axe` in CI as 03 says;
  contrast was designed for 4.5:1 but not measured.
- Not checked: phone widths, real data volumes, keyboard flows end to end.

## Decisions design made (confirm or change)

1. Theme, fonts and status colours in `globals.css`.
2. Tenant settings, members, tenant mappings and tenant tokens live in one tenant page, **Access and settings**;
   system-wide things live in **Admin**.
3. Review is full screen (no nav) with the apply panel on the right; the wizard is full screen with a step rail.
4. Edit forms for pools, monitors and providers are side sheets; virtual services get full pages with tabs.
5. Light is the default theme; dark mockups exist for every screen.

## Copy to keep

"Draft by <first name>, <HH:MM>" default draft name · "Review & apply" · "Request approval" · "Live" badge ·
"Reverted at canary" / "Retrying after error" / "Dead · gave up" · "Drifted: running config differs from revision #N" ·
"Metrics unavailable" + reason · "Host state not reported" · "Labels aren't supported yet" · "You wrote this change set,
so you can't approve it" · "Approving needs the tenant-admin role".

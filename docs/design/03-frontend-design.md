<!-- Snapshot exported 2026-10-10 from the online doc: https://claude.ai/code/artifact/23e2b8ae-c97b-4555-ab71-66d5dfc0485f
     The online doc may be newer. Names updated to Switchyard (syctl, sy-agent). -->

# Switchyard — Frontend Design

Oct 10, 2026 · @Miguel Rodriguez

## Summary

The frontend is a React + TypeScript single-page app served by the controller, built around one idea: every edit lands in a visible change set that you review as a diff before it touches an SE. Companion to the [replacement plan](https://claude.ai/code/artifact/bf06390f-6cf2-4fa9-9128-b6e8286ecb93) and the [backend design](https://claude.ai/code/artifact/3698c8a8-230c-4787-998c-007be6665190).

Stack: React 19, TypeScript (strict), Vite, TanStack Router and Query, shadcn/ui (Radix + Tailwind), and a typed API client generated from the backend's OpenAPI spec.

UX principles:

- **Tenant first:** the tenant is part of every URL and always visible in the top bar, so nobody edits the wrong tenant by accident.
- **Draft, then apply:** edits never go live on save. A persistent change-set bar shows pending edits; Apply opens a review with the object diff and the rendered config diff per SE group.
- **Live, not refreshed:** health, BGP/VRRP state, member status and job progress stream in over SSE. Nobody needs to press reload.
- **Guided create, full edit:** a short wizard for new virtual services (VIP, ports, cert, pool, health check), and plain tabbed forms for editing every field afterward.
- **Explain the rendered result:** every object page has a "Rendered config" tab, so operators who know HAProxy can see exactly what their click produced.
- **Keyboard and density:** a command palette (Ctrl/Cmd+K) for jumping to any object, dense tables with saved filters, and light and dark themes.

## Navigation

Every page except Login and Admin sits under `/t/:tenant/`, so switching tenant swaps the whole app context, and a copied URL always opens in the right tenant.

> Diagram (sitemap · tenant-scoped sections and system admin): see the [online doc](https://claude.ai/code/artifact/23e2b8ae-c97b-4555-ab71-66d5dfc0485f).

## App shell

Three fixed regions frame every page: the tenant bar on top, the section nav on the left, and the change-set bar at the bottom, which appears whenever a draft is open.

> Diagram (app shell wireframe · virtual service overview with an open draft): see the [online doc](https://claude.ai/code/artifact/23e2b8ae-c97b-4555-ab71-66d5dfc0485f).

## Screens

Phase 1 needs about 15 screens. List pages share one table component, and object pages share one tabbed layout: Overview, Settings, Rendered config, History.

| Screen | Shows | Key actions | Phase |
| --- | --- | --- | --- |
| Login | Enabled identity providers (Authentik, Entra ID, …), break-glass link if enabled | Pick a provider | 1 |
| Tenant home | Health summary, failing members, expiring certs, recent changes, running jobs | Jump to problems | 1 |
| Virtual services list | Name, VIP:ports, protocol, SE group, health, RPS, p95, 5xx, cert expiry | Filter, create, enable/disable | 1 |
| Virtual service detail | Routes (host/path → pool), live member status, latency breakdown, log tail, rendered `frontend` | Edit, drain a member, add a route | 1 |
| Create virtual service wizard | Steps: basics → VIP from a pool → ports and TLS → routes and pools → health check → review | Add to change set | 1 |
| Pools | Members with live state, algorithm, persistence, monitors | Enable/disable/drain at runtime, edit | 1 |
| Health monitors | Type, settings, pools using it | Edit, test against one member | 1 |
| Certificates | Source, SANs, expiry, VSes using it | Import, issue (ACME or Origin CA), renew now | 1 |
| Policies | HTTP rules, redirects, source allow lists | Edit with a rule builder | 1 |
| Networks and VIP pools | Subnets, VNets, IP usage bar, allocations | Create network, add range, release IP | 1 |
| SE groups | VMs per Proxmox host, HA mode, config hash per SE, agent status | Deploy, add/remove VM, redeploy one SE | 1 |
| Routing profiles | BGP peers with session state, or VRRP state per VIP; static routes | Edit peers, view advertised prefixes | 1 |
| Change sets | Open drafts, applied history | Review, apply, discard | 1 |
| Revisions | Revision timeline with author and summary | Diff any two, roll back | 1 |
| Jobs | Running and recent jobs with logs | Retry, cancel | 1 |
| Admin: tenants, identity providers, role mappings, API tokens, audit log | System-wide settings | Create tenant, map groups to roles, revoke tokens | 1 |
| WAF, rate limits, GSLB, Kubernetes | — | — | 2–3 |

## Change-set workflow

The change-set bar is the one place where config goes live. Everything else is drafting or runtime-only actions, and those are labelled as such.

1. **Start editing:** the first edit in a tenant opens a draft automatically (named "Draft by Miguel, 10:42", rename any time). The bar at the bottom shows the draft and its count of pending changes. Edited fields and objects get a dot marker in lists and forms.
2. **Keep working:** you can edit several objects across pages in one draft. Drafts survive logout because they live on the server. Other users' drafts in the same tenant are listed but don't affect what you see.
3. **Review:** "Review & apply" opens a full-screen review with three tabs:
   - **Objects:** each changed object as a field-level before/after.
   - **Config:** the rendered diff per SE group (Monaco diff view).
   - **Impact:** which VIPs, SEs and hostnames are touched, plus warnings from the validator, such as a cert that doesn't cover a hostname.
4. **Apply:** confirm with a summary line, which becomes the revision message. Progress streams live: lock → render → validate → canary SE → health check → remaining SEs → commit. On failure you see the stage that failed and its error, and the draft stays open to fix and retry.
5. **Conflicts:** if someone else applied a change to the same object first, the review shows a three-way view (base, theirs, yours) per field. You pick a value per field before you can apply.
6. **Roll back:** from Revisions, "Roll back to #42" creates a pre-filled draft and opens the same review, so a rollback is checked like any other change.

Runtime actions (drain or disable a member, renew a cert, deploy an SE VM) skip the change set. They need a confirm dialog, appear in the audit log, and carry a "Live" badge so their immediate effect is clear.

**Optional approval (four-eyes).** A tenant-admin can turn on "Require approval" per tenant, optionally limited to changes that touch production SE groups. With it on, the review screen's Apply button becomes "Request approval": the draft is frozen as `pending_approval` and approvers in that tenant are notified (in-app, plus email or webhook if configured). An approver, who must be a different person from the author, sees the same review and can approve (which applies it) or reject with a comment (which reopens the draft). Any edit after a request voids the approval. Runtime actions and system-admin break-glass applies bypass approval but are flagged in the audit log. Turning the toggle on or off is itself an audited action, and the tenant settings page shows who changed it and when.

## Frontend architecture

The server owns all state; the UI caches it with TanStack Query and patches the cache from the SSE stream, so there is no client-side store to keep in sync.

| Concern | Choice | Why |
| --- | --- | --- |
| Build | Vite, TypeScript strict, pnpm | Fast builds; output is static files served by the controller's API container |
| Routing | TanStack Router (file-based, typed search params) | Tenant and filters live in the URL, type-checked, so every view is linkable |
| Server state | TanStack Query | Caching, retries, optimistic updates for runtime actions |
| API client | `openapi-typescript` + `openapi-fetch`, generated in CI | Backend model changes become compile errors, not runtime bugs |
| Forms | React Hook Form + Zod, with schemas generated from OpenAPI (`orval`) | Same validation rules client and server; server field errors (problem+json paths) mapped onto inputs |
| Components | shadcn/ui (Radix primitives + Tailwind v4) | Accessible primitives we own in-repo, not a locked dependency |
| Tables | TanStack Table + virtualized rows | Sorting, column choice and saved filters; stays fast at thousands of rows |
| Diffs and config | Monaco editor (read-only diff view) with HAProxy syntax highlighting | Familiar review experience for the rendered config |
| Charts | uPlot for small live charts; Grafana panels embedded for deep dives | Light native sparklines, without rebuilding Grafana |
| Command palette | `cmdk` | Ctrl/Cmd+K jump to any object by name |

Grafana behind the controller (built, opened in its own tab, [ADR 0080](../adr/0080-grafana-behind-controller.md)): Grafana runs behind the controller with `auth.proxy`, so a logged-in user reaches it without a second login. The UI asks the controller for a one-time link (`POST /tenants/{t}/grafana/launch`); opening it starts a short proxy session for that tenant. Each tenant gets its own Grafana organization, and its data sources point at the controller, which rewrites every Prometheus and Loki query with `tenant=<id>` (the controller does this itself rather than through `prom-label-proxy`). A tenant can't see another tenant's metrics even if a dashboard is edited by hand, or someone reaches Grafana directly.

### Data and live updates

- **Query keys mirror API paths** (`['t', tenant, 'virtual-services', id]`), so invalidation after an apply is one prefix call.
- **One SSE connection per tab**, subscribed to the current tenant. Events (`member_status`, `vs_health`, `bgp_session`, `job_progress`, `revision_applied`) patch the cache directly with `setQueryData`, so the stream is not a refetch storm.
- **Reconnect** with backoff, then a full refetch of visible queries to cover missed events; a banner shows when the stream is down.
- **Draft overlay:** objects are fetched with `?change_set=<draft>` while a draft is open, so pages show the drafted state and mark which fields differ from live.

### Auth and RBAC

- **Login:** the backend handles OIDC and SAML and sets an HttpOnly, SameSite=Strict session cookie, so the SPA never touches tokens. Every write sends a CSRF header.
- **Permissions:** `/me` returns the user's roles per tenant. A `can(verb, objectType)` hook hides or disables controls, with a tooltip saying which role is needed. The backend still enforces every check; the UI only avoids dead ends.
- **Session expiry:** an expired session opens a re-login modal and keeps unsaved form input; drafts are already server-side.

### Quality bars

- WCAG 2.2 AA: keyboard reachable, visible focus, and status shown with text or icons, never color alone.
- Light and dark themes from CSS tokens.
- English only for now; strings go through one `t()` helper so i18n can be added later without a rewrite.

## Design handoff (Claude Design)

Screens are designed in Claude Design against a Switchyard design system built from the shadcn/ui theme, so every mockup uses only components and tokens the code already has.

### Source of truth

- **Tokens live in code:** the shadcn theme is a set of CSS variables in `globals.css`, in OKLCH, for light and dark: `background`, `foreground`, `card`, `popover`, `primary`, `secondary`, `muted`, `accent`, `destructive`, `border`, `input`, `ring`, `chart-1`…`chart-5`, `sidebar-*`, `radius`. The base theme is picked once with tweakcn or shadcn/ui Create and committed.
- **Switchyard additions:** status tokens `status-healthy`, `status-degraded`, `status-down`, `status-draining`, plus `draft` (change-set marker), `live` (runtime-action badge) and `pending-approval`. Each pairs with an icon and a label, because color alone never carries meaning.
- **Design System artifact:** an "Switchyard Design System" artifact (Claude's Design System type) holds these tokens for both themes, the type scale, spacing and radius, live previews of every component below, and the usage rules. Claude Design reads it for every mockup, so nobody restates the system in each prompt.
- **Figma (optional, for anyone working in Figma):** use a kit whose Figma variables carry the same names as the CSS variables. [shadcncraft Free Starter Kit](https://ui.shadcn.com/docs/figma) has native variables for all shadcn styles and imports tweakcn themes. Obra shadcn/ui Community Edition is the other maintained free kit; both have paid Pro versions with blocks. Keep Figma variable names identical to the CSS names so the kit, the Design System artifact and the code stay in step.

### Component inventory

| Layer | Components |
| --- | --- |
| shadcn/ui, used as-is | Button, Input, Textarea, Select, Combobox, Checkbox, Switch, Radio Group, Tabs, Data Table, Dialog, Alert Dialog, Sheet, Dropdown Menu, Command, Sonner (toasts), Badge, Tooltip, Card, Sidebar, Breadcrumb, Form, Alert, Skeleton, Progress |
| Switchyard custom (registry `@switchyard`) | ChangeSetBar, DraftMarker, ReviewDiff (objects / config / impact tabs), ConflictResolver, StatusPill, LiveBadge, MemberStateList, IpUsageBar, BgpSessionChip, VrrpStateChip, WizardStepper, ApprovalBanner, JobProgress, RenderedConfigView, TenantSwitcher |

### Workflow

1. **Design system first:** build the Design System artifact from the committed shadcn theme plus the Switchyard tokens and custom components.
2. **Screens per milestone:** design each milestone's screens (U1 first) in Claude Design from the Screens table. Every screen shows all of its states: empty, loading, error, open draft, pending approval, and read-only for missing permissions. Designs are checked at 1280 px and 1920 px wide in both themes.
3. **Review:** comments on the design, then sign-off per screen.
4. **Build:** in Claude Code with the shadcn MCP server (`pnpm dlx shadcn@latest mcp init --client claude`), which installs shadcn components by name. The Switchyard custom components are published to a private registry declared in `components.json` as `@switchyard`, so designs, code and the MCP server use the same component names.
5. **Parity check:** Storybook stories for every component in the inventory, with screenshot tests in both themes, are compared against the design system previews.

## Repo layout, testing and documentation

The UI lives in `ui/` of the same monorepo, so an API change and its UI change ship in one merge request, and CI regenerates the client to catch drift.

```text
ui/
  src/
    api/          generated client + query hooks per resource
    routes/       file-based routes: /t/$tenant/virtual-services/$id ...
    features/     change-set bar, review, wizard, live status, command palette
    components/   shadcn/ui components + shared table, form fields, diff view
    lib/          SSE client, permissions (can), formatting, theme tokens
  tests/e2e/      Playwright specs
  .storybook/     component stories
```

### Testing

- **Unit and component:** Vitest + Testing Library for hooks, form validation, the permission helper and every shared component. Against MSW-mocked API responses built from the OpenAPI examples.
- **Live updates:** tests feed scripted SSE events through MSW and assert cache patching and reconnect behavior.
- **End to end:** Playwright against the backend's Docker Compose stack with a fake SE (HAProxy + agent containers). Covered flows: log in, create a VS with the wizard, review the diff, apply, drain a member, roll back, and a conflicting apply between two users.
- **Accessibility:** `axe` checks in component tests and in every Playwright page visit; CI fails on serious violations.
- **Visual:** Storybook stories for shared components, with screenshot diffs in CI for both themes.
- **Gates:** typecheck, ESLint, Prettier, an 80% coverage floor on `features/` and `lib/`, and a CI check that the generated client matches the backend spec.

### Documentation

- TSDoc on exported hooks and components, and Storybook as the component reference.
- `docs/ui/` covers the change-set UX rules, how to add a new object type (route + list + form + rendered tab), and the SSE event catalog.

## Milestones and open questions

UI milestones trail the matching backend milestone by about one step, so each screen is built against a real API instead of mocks that drift.

1. **U1 (with backend M1–M2):** app shell, login, tenant switcher, change-set bar and review, VS/pool/monitor/cert list and detail pages, VS wizard, rendered config tab, revisions and rollback.
2. **U2 (with M3):** live status over SSE, SE group and routing profile pages with BGP/VRRP state, jobs page, tenant home dashboard.
3. **U3 (with M4):** networks, VIP pools and IPAM, SE group deploy and scale, Proxmox host placement view.
4. **U4 (with M5):** certificate issuance (ACME, Origin CA), the Cloudflare allow-list toggle, admin pages for identity providers, role mappings, API tokens and audit.
5. **U5+:** WAF, rate limits, Kubernetes connector, GSLB.

### Open questions

- [x] Backend addition needed: `GET` on any object with `?change_set=<id>` returning the drafted state. Add it to the backend API design?
  - [x] Yes.
  - [x] Added to the backend design under API conventions.
- [x] Component library: shadcn/ui (own the code, Tailwind) or Mantine (more ready-made, less to maintain)? This doc assumes shadcn/ui.
  - [x] Pros/Cons?
  - [x] **shadcn/ui:** the component source is copied into the repo, so nothing upstream can break or deprecate it, and every component can be changed freely. Built on Radix, which has strong accessibility. Tailwind styling is easy to theme. Cost: you maintain the components, and complex ones (date pickers, multi-select, rich tables) are assembled from parts. **Mantine:** 100+ ready components plus forms, dates, notifications and hooks, so you write less code and get good defaults. Cost: an upstream dependency with major-version migrations, harder to restyle deeply, and its own styling system beside anything else. Recommendation: shadcn/ui, because this UI is mostly tables, forms and diff views, which TanStack Table, React Hook Form and Monaco cover anyway. Mantine wins if you want screens up fastest with the least component work.
  - [x] Let's do shadcn. We will toss frontend to Claude Design, so it needs to know about its figma kits, how to work with it, etc.
  - [x] Done: shadcn/ui is the choice. The new Design handoff section covers the Claude Design design system, the Figma kits, the component inventory and the design-to-code workflow.
- [x] Should a tenant-admin be able to apply alone, or require a second person's approval (four-eyes) for production tenants?
  - [x] Make this an optional feature, that can be toggled on/off as necessary. This means we need to make sure auditing/logging is done.
  - [x] Done: a per-tenant "Require approval" toggle, with every request, approval, rejection and toggle change audited. See "Optional approval" under Change-set workflow; the backend design is updated to match.
- [x] Is Grafana embedding acceptable (needs anonymous or proxied auth to Grafana), or should all charts be native?
  - [x] Acceptable.
  - [x] Noted under the architecture table: Grafana sits behind the controller's auth proxy, with tenant isolation enforced on the data path.

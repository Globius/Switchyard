# 07: Built UI vs Design artboards (handoff v46)

Date: 2026-10-10. Scope: `ui/` at `origin/main` (34f8af2 and later) against the Switchyard UI handoff v46
(`switchyard-ui-handoff.zip`: README, 29 artboards in `design-source/`, screenshots at 1280/1920, `states/`),
`03-frontend-design.md` and `04-frontend-scope.md`. The system-wide planned-vs-built audit is
`06-planned-vs-built.md`; this file covers only the UI and does not repeat it.

## How this was checked, and its limits

- **Method:** every artboard's text and structure was compared with the code of the screen that implements it
  (strings, controls, states, API calls). A text-coverage scan of all 29 artboards against `ui/src` found the
  strings that appear nowhere in the code; each hit was then read in the code. Sample data (names, dates, IPs)
  is ignored.
- **Not done:** a side-by-side visual comparison. The app answers at `localhost:5173` and the controller is up,
  but every screen except `/login` needs a session and I had no break-glass login. The one screen I could see,
  Login, matches the screenshot (card, wording, warning banner, buttons). Visual status below is therefore
  "from code"; spacing, colour and layout differences are not covered. A later live check against `localhost:5173` (tenant `demo`, text only, no pixel comparison, see "Live check") confirmed the items it covered.
- **Status words:** **matches** = the designed content, controls and states are built. **differs** = built, but
  something designed is missing or done another way (listed). **missing** = not built.
- **Why** codes: *no API* (backend does not offer it), *design question* (artboard and README or API disagree,
  or the design left it open), *not built* (planned, not done yet), *overlooked* (backend supports it, design
  has it, build skipped it).
- The cause was inferred from `schema.d.ts` and the code, not from conversations with the build thread.
  Where I could not tell if an API exists, it says "check API".

## Summary

| | Count |
| --- | --- |
| Artboards | 29 (27 screens, plus `Main` and `Overlays` covered under Foundations) |
| Screens that match (cosmetic differences at most) | 9: Login, Tenant home, Change sets, Pools, Policies, Routing profiles, Admin tenants, Admin users, Admin role mappings |
| Screens that differ (built, with gaps) | 18 |
| Screens missing entirely | 0 (every screen has a route; only unknown paths show `NotBuilt`) |

Biggest gaps, in the order I would close them:

1. **Live data is only half wired.** The SSE stream only drives the top-bar indicator. It does not patch query
   caches (`status`, `vs_state`, `job_progress`, `revision_applied`), there is no stale/"Reconnecting" banner and
   no refetch on reconnect. Live pages poll instead. (Foundations, States.)
2. **Review Config tab has no Monaco diff.** It is a plain line list (`RenderedConfigView` says "Monaco arrives
   with the review diff"). Same for the Rendered config tab.
3. **Session expiry has no modal.** A 401 `session_expired` redirects to `/login` (and returns). The designed
   modal that keeps form input is not built (the code comment says so).
4. **ShowOnceSecret does not exist as a component.** Tokens and enrollment tokens show "Copy it now" but there
   is no "I stored it" check; break-glass password/TOTP has no QR and no stored check.
5. **VS History is a flat list**, not the designed timeline (filters, per-op field diff, draft entry,
   runtime actions, approvals, pool/monitor/cert toggle).
6. **VS Settings lacks the "Policies and access" section** (allow list, enforce toggle, restore client IP header)
   although `settings-model.ts` already carries those fields; no VIP suggestion; no "Revert all", no "Saved to
   draft at … version N".
7. **SE groups misses placement on Proxmox, HA mode, host-spread warnings, agent version, "Host state not
   reported", Register-existing as a distinct flow and the drift "Re-apply revision #N" / "View running config".**
8. **No shadcn components or `@switchyard` registry.** `ui/src/components/ui` has 7 hand-written files; dialogs,
   selects, tabs, tables, toasts, command are hand-rolled or missing. No React Hook Form, Zod, TanStack Table,
   Sonner or Monaco in `package.json`. No axe in CI (03 requires it).

## Foundations and shared components (artboard `Main`, `States`, `Overlays`)

| Item | Status | Why | What closing it takes |
| --- | --- | --- | --- |
| Theme tokens (`globals.css`, light/dark, status tokens) | matches | | Only the font family name differs (`Geist Variable` for fontsource); fine. |
| Geist and Geist Mono | matches | | Loaded through `@fontsource-variable`. |
| Dark/light toggle, system default | matches | | Toggle in top bar and user menu. Design says light is the default; verify the default is not "system". |
| shadcn/ui primitives (Dialog, Select, Combobox, Tabs, Table, Dropdown, Sheet, Command, Sonner, Tooltip, Form…) | differs | not built | Only Badge, Button, Popover-menu, Sheet, Skeleton, Switch, Tooltip exist. Others are inline or hand-rolled. Run `shadcn add` and port, or record an ADR that hand-rolled is accepted. |
| React Hook Form + Zod from OpenAPI | missing | not built | Forms use ad-hoc `useState` and 422 mapping (`Findings.tsx`). Adopt RHF + Zod, or amend 03. |
| TanStack Table | missing | not built | Tables are plain `<table>`; no column sizing/sort UI beyond the list queries. |
| Monaco (rendered config, review diff) | missing | not built | `RenderedConfigView` is a `<pre>`; `monaco-editor` is not a dependency. Needed for HAProxy highlighting and range highlight. |
| Sonner toasts | missing | overlooked | No toast anywhere. Runtime actions and saves report inline only. Add Sonner. |
| StatusPill (icon + text, all vocabulary) | matches | | `components/sy/StatusPill.tsx`. |
| DraftMarker | differs | design question | A dot with `aria-label` is used (`VsSettings`), struck-through deleted rows and "New" appear on list pages; no shared component. Extract. |
| LiveBadge | matches | | Used on member actions. |
| ChangeSetBar (counts, problems, Review & apply, discard, author kinds person/token/break-glass) | matches | | `shell/ChangeSetBar.tsx`; token and break-glass pills built. Verify bar is fixed at the bottom on all screens. |
| WizardStepper | differs | design question | Built as an `<ol>` rail (wizard only); SeGroups and Certificates open dialogs without a stepper. Matches the artboards, which use it only in the wizard. |
| ApprovalBanner | differs | not built | Approval text is a block inside the Review apply panel and a note in Settings; no shared banner on other pages. "Pending approval: draft frozen" (States) is not built: Settings says "Saving a change withdraws the request" instead. README says both ("Any edit withdraws a pending request" and States says "Editing is off while approval is pending"); design needs to pick one. |
| JobProgress (stages) | matches | | `review/JobProgress.tsx`. |
| RenderedConfigView (Monaco, range highlight, fingerprint) | differs | not built | Fingerprint shown; no Monaco, no range highlight. See above. |
| ConflictResolver (Base/Theirs/Yours, Both) | matches | | `review/ConflictResolver.tsx`, `conflict.ts`, list conflict included. |
| UnavailableState (icon, title, API reason) | matches | | Used for metrics and logs. |
| ShowOnceSecret (Copy, "I stored it") | missing | overlooked | Copy button exists in `Access.tsx`/`SystemTokens.tsx`; no "I stored it" gate. Add a shared component and use it for tokens, enrollment tokens, break-glass. |
| ChipInput (keyboard, empty, error) | matches | | `components/sy/ChipInput.tsx` has tests. |
| StreamIndicator (live / connecting / down) | matches | | Top bar; icon and label each state. |
| MemberStateList | differs | not built | Inline in `VsOverview` and `Pools`; admin state only, per README. No shared component. |
| IpUsageBar | differs | not built | Built inline in Networks (bar plus n of m used); not a shared component. |
| BgpSessionChip, VrrpStateChip | differs | no API (check) | BGP chip exists in SeGroups; VRRP state chip does not (no per-engine VRRP state found in the schema). |
| Permissions: `can(verb, object)` from `GET /permissions` | matches | | `lib/permissions.ts`; disabled controls with tooltip. |
| Session: cookie, CSRF, 4 h / 30 min, Refresh access | matches | | `auth/session.ts`, `UserMenu`. |
| Session expired modal keeping form input | missing | not built | `main.tsx` redirects to `/login?error=session_ended&next=…` with a comment "until the re-login modal is built". Draft is kept on the server; unsaved form input is lost. |
| SSE: patch caches from `status`, `vs_state`, `job_progress`, `revision_applied` | missing | not built | `shell/stream.ts` only tracks connection state. Pages rely on `refetchInterval` (6 places). |
| Stream-down banner with "Reconnecting in N s / Retry now", refetch on reconnect | missing | not built | Only the top-bar chip changes. Add banner, backoff, `invalidateQueries` on `onopen`. |
| `observed_at` on live values | differs | overlooked | Shown on Home and SeGroups headings only; not on VS members, pool members, status chips. |
| Standard states: skeleton, empty, error, read-only tooltip, 412, 422 summary | matches | | `states.tsx`, `Findings.tsx`. |
| 422 summary with several findings linking each field | matches | | `components/sy/Findings.tsx`. |
| Accessibility (axe in CI, 4.5:1 contrast, keyboard flows) | missing | not built | No axe dependency or test. 03 requires it. |
| Narrow screens (nav stacks) | differs | design question | Responsive classes exist on several pages; the shell nav behaviour at <1024 not verified. README says phone widths were not checked by Design. |
| Overlays: tenant switcher | matches | | Search, approval badge. |
| Overlays: command palette (Ctrl/Cmd K) | differs | design question | Built: object search grouped by type, "New virtual service", "Review my draft". Artboard lists more actions (verify list against `Overlays`). |
| Overlays: user menu | matches | | Roles via group, session end time, Refresh access, theme, Admin, Sign out. |

## Screens

| Screen (artboard) | Status | What differs | Why | What closing it takes |
| --- | --- | --- | --- | --- |
| **Login** (`Login`) | matches | Break-glass block shows whenever `/auth/providers` says `break_glass: true`, as designed. Logo glyph differs from the artboard (small). The four error states and "Your session ended" return are built. Seen live. | | Swap the logo glyph if wanted. |
| **Tenant home** (`TenantHome`) | matches | Stat tiles, failing members, expiring certs, running jobs, drifted engines, open drafts, recent revisions all present. "Last check" reason (`L4TOUT`, `L4CON`) in failing members and "since HH:MM · group" not seen in code. | no API (check) | Confirm the summary carries last-check reason; add if so. |
| **Virtual services list** (`VirtualServices`) | differs | Filters (name, SE group, protocol, enabled), sort, URL state, cursor "Load more", metrics-unavailable reason, label filter disabled with the designed tooltip: built. Not found: per-row draft markers, "New" and struck-through deleted rows (the count line is draft-aware). Cert expiry is a sort only, not a column. Pages of 50 with "Load more" as designed. | overlooked | Verify row draft markers with `?change_set`; add if missing. |
| **VS Overview** (`VirtualServiceDetail`) | differs | Built: header with health, routes, members with drain/enable/disable (live, confirm, partial failure), certificates, listener, rendered frontend, access log, latency (unavailable state), invalid-draft card with Go to field and "Show the live version". Missing or different: "Open in Grafana" is a text hint, not a link (no link config); no sparkline/RPS/p95/5xx tiles (designed as unavailable when no backend; built only as the unavailable message); no "Reorder" shortcut on the routes card (reorder is in Settings); no certificate-SAN mismatch warning on the Overview (it is a finding in Review); "N of M members up" in the header exists, per-member health absent by design. | no API (metrics, logs, grafana URL) / overlooked (Reorder, SAN warning) | Add metric tiles when the backend reports `available: true`; add `grafana_url` setting (check API) and Reorder link. |
| **VS Settings** (`VirtualServiceSettings`) | differs | Built: Basics, Addressing, Ports (TLS/HTTP2/PROXY), Certificates and policy sets (ChipInput), Routes with move up/down and redirect, live value + Revert per field, draft dot, 412 conflict panel with "Reload and discard my input" and "Copy my values". Missing: **Policies and access** (allow list, enforce toggle, restore client IP header; fields are in `settings-model.ts` but have no controls); VIP suggestion ("Suggest from a VIP pool", "Suggested, not reserved"); HTTPS redirect port; "Revert all fields"; "Saved to draft at HH:MM · version N"; "No attached certificate covers host" inline finding; drag-to-reorder (buttons used instead); "Changing protocol clears fields…" (protocol is read-only here, design lets you change it); UDP field disabling uses the schema, built. | overlooked (policies, redirect port, revert all, saved-at, VIP suggest); design question (protocol changeable?) | Add the Policies section and VIP suggest first; they are small and backed by the API. |
| **VS Rendered config** (`VirtualServiceRendered`) | differs | Draft/live switch, invalid-draft card, empty state built. Missing: Monaco with HAProxy highlighting; the file path header and fingerprint exist only as a fragment label; the "With my draft / Live" segmented toggle is a text line plus link, not a Toggle Group. | not built | Add Monaco and the segmented control. |
| **VS History** (`VirtualServiceHistory`) | differs | Built: a list of revisions and a list of audit events. Missing: segmented filter (Config changes / Approvals / Runtime), "Include its pools, monitors and certificates" switch, per-revision field diff (`+ routes[2]…`), the unapplied draft entry on top ("not yet live"), approval and rejection entries with comment, link to Review. | overlooked | The data comes from `revisions?object=` and `audit-events?object=`; build one merged timeline with the filters. Medium. |
| **Create wizard** (`CreateWizard`) | differs | Built: six steps (Basics, VIP, Ports and TLS, Routes and pools, Health check, Review), protocols HTTP/TCP/UDP, next-free suggestion with "Suggested, not reserved", "Suggest again", ops summary and atomic ops. Check vs artboard: the review step list of ops ("3 ops", "Adds to your draft"), "Checks run after you add these" explainer and where findings land are present as "These objects are added to your draft as one atomic set of ops"; the "same address in another draft" explanation and "Not on a TLS port" redirect hint were not found. Wizard uses a plain monitor picker without the "Up after 2 passing, down after 3" summary. Full screen with step rail: matches. | overlooked (explainers) | Copy the missing explanatory text from the artboard; low effort. |
| **Review and apply** (`Review`) | differs | Built: Objects / Config / Impact tabs, apply panel (revision message, warnings ack, request approval, approve, reject with comment, break-glass reason dialog), JobProgress, Reverted / Retrying / Gave up copy, conflict (ConflictResolver), invalid phase with findings, list diffs (`ListDiff`). Missing: Monaco diff in Config (plain lines now); "Open job" link from the apply panel; "You can leave this page" note; viewer-specific copy ("needs tenant-admin" on a disabled Approve: not found; "You wrote this change set, so you can't approve it" is built); Impact tab: allocation/release lines and canary target list are partial (Impact shows "What this touches" and the validator list); "Frees 10.40.12.30 back to vip-edge" line on deleted services not found. | not built (Monaco); overlooked (the rest) | Monaco first (shared with Rendered config); then Open job link and the two missing copy lines. |
| **Change sets** (`ChangeSets`) | matches | Open and history lists, author, approver, rejection comment, ops, `expects v{n}` all present. "Reopened after rejection" chip not seen. | no API (check) | Check if the change set carries a reopened flag. |
| **Revisions** (`Revisions`) | differs | Built: list, compare two, roll back dialog, "Create rollback draft". Missing: the git commit short hash per revision (`git 4c1e9a2`), "2 objects changed" count in the list, the note "The rollback is its own change set. Your open draft with N changes is not touched." | overlooked / no API (check for commit hash in `GET /revisions`) | Add count and note; add hash if the API has it. |
| **Jobs** (`Jobs`) | differs | Built: list with state/kind filters, details with stages, log, retry, cancel, Reverted/Retrying/Dead copy. Missing: "Open change set" link on apply jobs (shows `change set {id}`, not a link); relative duration column ("1 min 4 s", "12 min"); log lines with per-stage timestamps in design show; live update from `job_progress` (polling only). | not built (SSE) / overlooked | Wire `job_progress`, add links and durations. |
| **Pools** (`Pools`) | matches | Edit sheet, members with admin state, balance, persistence, UDP rules, Source-hash hint for WireGuard, monitor picker. Drain via runtime action stays on VS Overview as designed. | | Verify "Members (admin state)" column wording. |
| **Health monitors** (`HealthMonitors`) | differs | Built: list, edit sheet, "Used by" pools, UDP rule. **Test**: a test call exists, but the designed result panel (member picker, "from se-edge-a-01", failure reason, latency "2003 ms") should be compared; "Runs this monitor's settings as edited here, once…" text not found. | not built (U3) | Finish the test panel per artboard. |
| **Certificates** (`Certificates`) | differs | Built: list sorted by expiry, import (PEM/PKCS#12), issue (Cloudflare), renew now, replace, delete with "used by" guard, "keys live only in OpenBao". Missing: "Key matches the certificate" and "Valid now, until …" live validation in the import sheet (no pre-check API found); "Auto · 30 days before" renewal wording (shows `renew {policy}`); runtime/approval note that issue/renew open `system:certs` change sets with approval. | no API (check, validation) / overlooked (copy) | Add the approval note; add parsing preview if the API offers a parse endpoint. |
| **Policies** (`Policies`) | matches | Redirect and allow-list kinds, rule editor, CIDR editing with line errors. | | |
| **Networks** (`Networks`) | differs | Built: networks list, create/attach, VIP pools with range editing, usage "n of m", allocations table, Find IP or service, Apply SDN as a job, draft markers ("Freed by draft", "In a draft · not reserved"). Missing: "Edit service" link on an address row (VIP freed only through the service); an IpUsageBar; per-network "SDN apply running" chip and "Proxmox SDN job started with revision #41" line. | overlooked | Add link, bar and chip; small. |
| **SE groups** (`SeGroups`) | differs | Built: list, engines table, status (In sync / Drifted), deploy sheet (nodes, template ID, SSH keys, mgmt pool, count), enrollment token (once), register existing, retire and remove with distinct warnings, runtime-action permission note. Missing: **Placement on Proxmox** card (host spread, "Host state not reported", single-host warnings); **HA mode** field and routing-profile summary chip (`edge-bgp · BGP · AS 65040`); agent version per engine; "Waiting for agent / not enrolled" state per engine; **drift banner with "Re-apply revision #N" and "View running config"** (only a "Drifted" chip and "Reported only; nothing is changed automatically."); BGP session chips partly built. | no API (host state, agent version; check) / overlooked (re-apply, HA mode) | The drift re-apply is a README requirement ("the one offer"); do it first. Placement needs the Proxmox endpoints (see 05). |
| **Routing profiles** (`RoutingProfiles`) | matches | BGP and VRRP kinds, priorities, VRID, BFD, static routes empty state. The "se-core-01 starts at priority 150" per-engine hint was not found. | | Copy the per-engine priority hint. |
| **Tenant access and settings** (`TenantAccess`) | differs | Built: members, role mappings, tokens (create, revoke, copy once), approval switch, quotas. Missing: member "last sign-in" and provider/group column format; "On since Oct 6, by …" attribution on the approval switch; tokens use "Copy it now" without "I stored it". Tabs (Members, Role mappings, API tokens, Settings) match the artboard. | overlooked | Shared ShowOnceSecret; the attribution needs `approval_changed_by/at` in the tenant (check API). |
| **Admin: tenants** (`AdminTenants`) | matches | Create/edit/delete, quotas, approval toggle, links to users and mappings. "Turned on by … on …" attribution not found. | no API (check) | As above. |
| **Admin: users** (`Users`) | matches | List with filters, user details panel, "What {name} can do now", disable/enable/end sessions/delete with confirm. | | |
| **Admin: identity providers and break-glass** (`IdentityProviders`) | differs | Built: list, add/edit, test sign-in, SP metadata, break-glass: turn on/off with reason and duration, set up administrator (password and TOTP). Missing: OIDC "Discovery document found" check and "From URL / Paste XML" for SAML; "Replace secret" as write-only field on edit; claims view ("Show all claims", mapped roles preview); QR code from the otpauth URI and "I stored it" check in break-glass setup (no QR library); "forced by the deployment" state is built. | overlooked (QR, stored check); no API (check) for discovery/XML parse | Add QR and ShowOnceSecret; add SAML XML paste and discovery if the API supports them. |
| **Admin: role mappings** (`RoleMappings`) | matches | System-wide and per-tenant, "What each role can do" matrix from `GET /permissions`. | | |
| **Admin: system tokens** (`AdminTokens`) | differs | Create, revoke built. Missing: "I have stored this token" gate, "expires in 4 days" relative text on rows. | overlooked | ShowOnceSecret. |
| **Admin: audit log** (`AuditLog`) | differs | Filters (tenant, action, time range, search), details expander, export, load more. Missing: actor filter, custom range inputs ("Custom range" option), request ID column (`req_8d21…`). | overlooked (check API fields) | Add fields the API already returns. |
| **Standard states** (`States`) | differs | Loading, empty, error, read-only (403), 412, 422 summary: built. Missing: session-expired modal, stream-down banner, pending-approval frozen draft, partial failure "Retry on {engine}" button (Review has a retry action; member drain shows "Applied on N, failed on M" without a retry button), drift panel with hashes and "View running config". | not built | See Foundations. |

## Behaviors from the README not covered above

| Behavior | Status | Note |
| --- | --- | --- |
| Pages read with `?change_set=` while a draft is open; drafted fields show the dot; deleted struck through | matches | Settings and lists use the draft read. |
| Objects tab: changed fields only; live vs draft | matches | `review/model.ts`. |
| 412 conflict builds base/theirs/yours | matches | `conflict.ts`. |
| Approval tenant: Apply hidden, Request approval, author/operator rules | differs | Request approval, "You wrote this" built; disabled Approve tooltip "Approving needs the tenant-admin role" not found. |
| Break-glass applies need a reason | matches | Dialog in Review. |
| Rollback opens a pre-filled draft and review | matches | Revisions. |
| Runtime actions: confirm, Live badge, audited, partial failure | differs | Member state done; engine, token, certificate actions confirm dialogs built; Live badge only on members. |
| Drift: report only; one offer "Re-apply revision #N" | differs | No re-apply offer. |
| UDP: forms disable fields per schema | matches | `useProtocolRules` in Settings; wizard has UDP rules. |
| Secrets never shown | matches | Write-only fields, tokens once. |
| "Host state not reported" | missing | Not found in code (SeGroups placement not built). |
| "Labels aren't supported yet" | matches | VS list. |
| Copy strings (Draft by …, Review & apply, Live, Reverted at canary, Retrying after error, Dead · gave up) | matches | Present. Missing: "Host state not reported", "Suggested, not reserved" is present; "Go to field" present. |

## Suggested order to close

1. SSE cache patching, stale banner, reconnect refetch, session-expired modal (cross-cutting; one MR each).
2. Monaco for Review Config and Rendered config.
3. ShowOnceSecret component (tokens, enrollment, break-glass) plus QR.
4. VS Settings: Policies and access, VIP suggest, Revert all, saved-at.
5. VS History timeline.
6. SE groups: drift Re-apply, placement, HA mode.
7. Small copy and link gaps (Review, Jobs, Revisions, Networks, Wizard explainers).
8. Decide: shadcn registry and RHF/Zod adoption, or amend 03 with an ADR; add axe to CI.

Open design questions found: editing while approval is pending (frozen vs withdraws the request); protocol
changeable in Settings; Grafana link source; whether Access and settings is tabs or sections.

## Visual pass (Playwright, 1280 px, light)

Screenshots of 24 routes were taken with Playwright against the dev controller (tenant `demo`) and 3 pairs were compared with the artboards (Tenant home, VS Overview, VS Settings). The rest are captured but not yet compared. The dev tenant has no draft, engines, drift or findings, so those states cannot be seen live.

- **Tenant home:** layout, tiles, cards and nav match; differences are data only (empty chips, no failing members) plus the top-bar divider after the logo, and the avatar shows `B` instead of two initials.
- **VS Overview:** layout matches (header, routes, members, listener, certificates, access log, rendered frontend, latency). Differences: no per-route row menu (`⋮`) in the Routes table; unavailable states (logs, metrics) are plain lines, not the dashed centred card; health shows `Unknown` (no engines); the Certificates chip shows `4099 days` as a neutral pill, no status icon.
- **VS Settings: differs a lot.** Built is a single narrow column of native inputs (native checkboxes, `<select>`, no Switch, no section cards). The artboard has cards (General, Listener, Routes, Policies and access), a two-column layout, a Fixed/Suggest VIP radio, a ports summary table with `Edit ports`, a routes table with columns (Host, Path match, Path, Action, Target) and a footer with `Revert all fields` / `Save to draft`. The built Routes block has no Path match column. The built sticky `Add to change set` bar overlaps the Ports section at 1280x900 (may be a full-page-capture artefact; worth a check at a normal scroll). Closing it is a restyle plus the missing sections listed above.

## Live check (text only)

Signed in as break-glass on a dev controller (tenant `demo`, 2 services, 1 SE group, no engines). The browser pane
would not render screenshots, so this compared page text, not pixels. Confirmed: VS Settings has Basics, Addressing,
Ports, Certificates and policies, Routes and no "Policies and access" section, no VIP suggestion, no Revert all;
SE group page has Settings, Networks and an empty engines list but no placement, HA mode or drift panel (drift
needs an engine to show); top bar shows "Stream live"; the shell nav matches the handoff map. Pixel-level spacing
and colour were not compared.

## Visual differences (Playwright, built vs artboard, 1280 px)

Side-by-side comparison of 21 screens: artboard on the left, build on the right. The artboards show a populated
tenant (`globius-prod`) and often an open dialog or draft; the dev tenant (`demo`) is nearly empty. Items below are
differences in layout, styling or formatting, not data differences. States the dev tenant cannot show (draft,
drift, findings, dialogs) are covered by the code-based tables above.

| # | Screen | Difference | Severity |
| --- | --- | --- | --- |
| V1 | VS Settings | Single narrow column of native inputs. Artboard: cards (General, Listener, Routes, Policies and access), two columns, Switch for Enabled, Protocol toggle group, Fixed/Suggest VIP radios, ports summary table, routes as a table (Host, Path match, Path, Action, Target, remove icon), footer with Revert all / Save to draft | High |
| V2 | VS Rendered config | Built stacks every rendered file in small type. Artboard: one file with engine select, path, fingerprint, Copy, Live / With my draft toggle, line numbers from context, highlighted range | High |
| V3 | VS History | Two flat lists. Artboard: timeline with segmented filter, include-objects switch, inline diffs, status chips (In draft, Live, Approval), Compare buttons | High |
| V4 | Policies, Routing profiles | Built is a list that opens a sheet. Artboard is master-detail (list left, editor right with rule cards, inline validation, Revert / Save to draft) | High |
| V5 | Audit log | Older rows show only `HH:MM:SS` (artboard `Oct 6 14:02`); actor shows `user:local:breakglass` (artboard a name); object shows raw ids (`change_set:01a1285d-…`) where the artboard has readable names; request id is a long hash (artboard short `req_8d21…a0f`); Break-glass pill sits under the action; rows are tall | Medium |
| V6 | Revisions | Author is raw `user:authentik:dana`; timestamps absolute locale (`10/10/2026, 8:52:04 PM`) instead of `today 08:12`; no git hash; Roll back button on every row (artboard has it in the compare panel) | Medium |
| V7 | Home, Revisions, others | Raw principal strings wherever an author is listed; artboard shows display names. Needs a principal-to-name lookup | Medium |
| V8 | Certificates | Expires cell wraps (`4099 days` pill plus `12/31/2037`); artboard shows a status pill (`12 days`) plus `Oct 22, 2026` on one line; US numeric dates; Name column wraps; extra Actions and Used by columns | Medium |
| V9 | Networks | `Apply SDN` sits in the Networks card header, not in the artboard. Draft rows (Freed by draft, In a draft), `Edit service` button and `SDN apply running` chip not present | Medium |
| V10 | SE groups | Empty group shows a centred empty state and a long hint. Artboard: engines table (State, Reachable, BGP sessions, Config, Observed, Disable / More), Placement card, drift banner | Medium |
| V11 | Create wizard | Header has no logo (artboard: logo plus tenant); step rail check marks for completed steps not verified beyond step 1; `So far` card and `Adds to your draft` panel only partly seen | Low |
| V12 | Admin tenants | List lacks Members and Quotas columns; detail lacks "4 people through 5 role mappings" and "Turned on by …" | Low |
| V13 | Role mappings | Matrix lists every permission (31 rows); artboard groups them into 8 rows. Add-mapping controls are a separate card above the table, not a footer row | Low (design question) |
| V14 | VS list | Matches, except health shows `Unknown`. Draft markers, `Modified` / `New` / `Deleted` pills and `Restore` need a draft to compare | Low |
| V15 | VS Overview | No per-route kebab menu; unavailable states are plain text, not the dashed centred card; Certificates chip lacks a status icon; `Open in Grafana` missing | Low |
| V16 | Top bar | Divider after the logo missing; avatar shows one initial where the artboard shows two | Low |
| V17 | Identity providers | Close; break-glass header lacks remaining-time text when timed; forced-on message is small plain text | Low |
| V18 | System tokens | List close. The shown-once dialog needs the "I have stored this token" check | Medium |
| V19 | Tenant home | Matches; `Jump to problems` is disabled when nothing fails (artboard shows it enabled) | Low |
| V20 | Login | Matches; logo glyph differs | Low |

## Backlog (numbered, for tackling)

Effort: S = under a day, M = a few days, L = a week or more. "Code" refers to the tables above, "V" to the visual table.

| ID | Item | Source | Effort | Depends on |
| --- | --- | --- | --- | --- |
| B1 | Adopt shadcn primitives (Dialog, Select, Switch, Tabs, Table, Toggle Group, Sonner), replace native controls, set up the `@switchyard` registry | Foundations, V1 | L | none |
| B2 | Monaco: rendered config view with range highlight and engine picker; Review Config diff | Foundations, V2, Review | M | none |
| B3 | VS Settings rebuild: cards, two columns, Policies and access, VIP radios, Revert all, saved-at, routes table | VS Settings, V1 | M | B1 |
| B4 | VS History timeline | VS History, V3 | M | none |
| B5 | Policies and Routing profiles as master-detail | V4 | M | none |
| B6 | SSE cache patching, stale banner, reconnect refetch | Foundations | M | none |
| B7 | Session-expired modal | Foundations | S | none |
| B8 | ShowOnceSecret component (tokens, enrollment, break-glass, QR) | Foundations, V18, IdPs | S | B1 |
| B9 | Display-name lookup and a shared date / relative-time formatter used everywhere | V5-V8 | S | API check for principal names |
| B10 | SE groups: placement, HA mode, engines table columns, drift Re-apply and View running config | SE groups, V10 | M | Proxmox endpoints |
| B11 | Networks: Edit service, draft rows, SDN chip, shared IpUsageBar | Networks, V9 | S | none |
| B12 | Review: Open job link, viewer copy, impact lines | Review | S | B2 |
| B13 | Jobs: change set link, durations, SSE | Jobs | S | B6 |
| B14 | Revisions: git hash, objects-changed count, rollback note, Roll back in the compare panel | V6 | S | API check |
| B15 | Audit log: date, names, readable object, short request id, actor filter, custom range | V5 | S | B9 |
| B16 | Certificates: layout and approval note | V8 | S | B9 |
| B17 | Admin tenants: Members and Quotas columns, attribution | V12 | S | API check |
| B18 | Wizard explainers and header | V11 | S | none |
| B19 | Polish: kebab menus, dashed unavailable card, top-bar divider, avatar initials, Jump to problems | V15, V16, V19 | S | B1 |
| B20 | Add axe to CI; decide on RHF/Zod and TanStack Table | Foundations | S | none |

Suggested sequence: B1 first (everything else restyles on top of it), then B6, B7, B8, B9 in parallel, then B2, B3, B4, B5, B10.

## Status after the build threads (completeness sweep, 2026-10-11)

Owners: **UI** = the build thread, **part 2** = the artboard parity part-2 thread, **backend** = blocked on an API.

| Item | Status | Owner / note |
| --- | --- | --- |
| V1 VS Settings | done | UI, cards, two columns, VIP radios, routes table, footer (!57) |
| V2 Rendered config | done | Monaco, HAProxy highlighting, range, Copy, With my draft / Live toggle. Engine picker shows the files one engine gets (group and engine scope, secrets redacted) |
| V3 VS History | done | UI (!61) |
| V4 Policies, Routing profiles | done | Policies by part 2 (!63); Routing profiles were master-detail already |
| V5 Audit log | part 2 | formatters in !60, remaining columns with part 2 |
| V6 Revisions | part 2 | !60 |
| V7 Names and dates | part 2 | !60 |
| V8 Certificates | part 2 | |
| V9 Networks | done | Edit service by part 2; per-network SDN chip from `/sdn/status` (!76) |
| V10 SE groups | done | engines table, placement, re-apply job, agent version. "View running config" on drifted engines: running HAProxy config in Monaco with its fingerprint against the desired one, and agent file hashes (hashes only) |
| V11 Create wizard | done | header logo and tenant, monitor summary ("Up after 2 passing, down after 3"); the other explainers were already in |
| V12 Admin tenants | done | Members column and "N people have access" (!76) |
| V13 Role matrix grouping | done | grouped by the `group` the server sends; open a group to see each permission (!76) |
| V14 VS list | done | |
| V15 VS Overview | done | Grafana link from `/me`, Reorder link on the routes card, dashed unavailable card, certificate status icon |
| V16 Top bar | done | divider added; initials come from the display name |
| V17 Identity providers | done | remaining-time text shipped; forced-on text styling is minor (UI) |
| V18 System tokens | done | ShowOnceSecret (!64) |
| V19 Tenant home | done | |
| V20 Login | deviation | logo glyph differs |
| B1 shadcn primitives, `@switchyard` registry | deviation | No artboard or the Foundations page shows an opened select or combobox other than the ChipInput list (built); the design source itself uses native `<select>` elements. Selects stay native; dialogs, sheets, switches, menus and the chip input are Radix-based. The `@switchyard` registry is not set up |
| B2 Monaco | done | !58 |
| B3 VS Settings | done | !57 |
| B4 VS History | done | !61 |
| B5 Master-detail | done | |
| B6 SSE patching, stale banner, refetch | done | !53 |
| B7 Session-expired modal | done | !65 |
| B8 ShowOnceSecret | done | !64 |
| B9 Names and dates | part 2 | |
| B10 SE groups | mostly done | see V10 |
| B11 Networks | part 2 | |
| B12 Review links and copy | done | "Open job" link and "You can leave this page" |
| B13 Jobs | done | change set link, Duration column, live updates |
| B14-B17 | part 2 | revisions, audit, certificates, admin tenants |
| B18 Wizard explainers | done | |
| B19 Polish | done | |
| B20 axe, RHF/Zod, TanStack Table | partly done | axe and keyboard and phone checks via Playwright (!69); RHF, Zod and TanStack Table not adopted (deviation, record an ADR if kept) |
| Backend-fed items | done | quota usage, revision message, per-member health and last check, host placement, audit export, BGP max prefixes |
| Pool passive checks, slow start, TLS profile per port, OCSP stapling | done | pool sheet, Edit ports sheet, certificate sheet (!76) |

## Admin screens, live check

Checked on 2026-10-11 against a local controller, signed in as the break-glass system-admin: Tenants, Users, Identity providers, Role mappings, System tokens and Audit log all load and match the artboards. One gap fixed: the Break-glass card showed an empty "Enabled" value when the deployment forces it on; it now says "Always on (set by the deployment)". Rechecked after rebuilding the controller from main (database migrated to head): actor names resolve through `/principals` and the role matrix shows its eight groups (Read, Applications, Infrastructure, Change sets, Runtime, Tenant settings, Access, System). The only remaining console 404 is the dev server's missing favicon.

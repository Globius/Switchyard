# Planned vs built

Date: 2026-10-10. Audit of `origin/main` (6eb0464) against the plan: 01 to 05, the ADRs and Design's v46 handoff
(`docs/design/ui-handoff`, kept out of Git). Built means what is on `main`: `controller/`, `agent/`, `proto/`, `syctl`,
`ui/`, `docs/ui/README.md`, `docs/api.md` and `docs/api/openapi.json` (105 paths).

Status: **done** (built and tested as planned), **partial** (built with a stated difference), **missing** (not built).
Why when not done: *no API*, *deferred* (the plan puts it in a later phase), *not started*, *design question*.
This is a report: nothing here is fixed by this MR. Evidence is the code and docs on `main`; the UI was read, not run.

## Summary

Phase 1 backend (M1 to M5) and M6 (UDP) are done. Every artboard of the v46 handoff has a screen. What is open falls in
four groups:

1. **No data source behind a built screen:** metrics and logs (API done, no pipeline), Proxmox host state, per-member
   health on the VS Overview, "last check" on failing members, quota usage counts.
2. **UI differences from the design:** Placement card, role matrix grouping, read-only revision message, plain-text
   config diff instead of Monaco, label filters, audit Export limited to loaded events, drift "Re-apply revision #N".
3. **Operations plan not started:** production deployment (Patroni, pgBackRest, controller image, management tenant,
   adopt import), SE image recipe, runbooks, controller metrics, Prometheus service discovery, CI (paused).
4. **Phase 2 and 3 (deferred by the plan):** WAF, rate limits, Kubernetes, GSLB, tag discovery, Avi importer, health score,
   packet capture, scale-out, tenant portal.

Never verified live: the `.p12` upload and the conflict view (tests with mocked responses only).

## 1. Backend capabilities (02, 05, ADRs)

| Item | Status | Why not done / note | What closing it takes |
| --- | --- | --- | --- |
| Domain models, schema, RLS, tenancy enforced twice (M1) | done | | |
| HAProxy renderer, golden tests, `haproxy -c` | done | | |
| Change sets, ops, draft reads, diff, impact, warnings, 412 conflict (M2) | done | | |
| Apply pipeline: lock, render, validate, canary, rollout, commit | done | | |
| Revisions, rollback, Git mirror job | done | | |
| Optional four-eyes approval, break-glass bypass, audit | done | | |
| `?apply=true` implicit single-op change set | done | | |
| Filters `name`, `q`, `se_group`, `protocol`, `enabled`, `sort`, cursor | done | `?label=` dropped: objects have no labels (04) | Labels need a model change, an ADR and a migration |
| Typed responses, per-object `/rendered`, policy and routing `kind` unions | done | | |
| Jobs: list, stages, log, retry, cancel; SSE `job_progress` | done | | |
| `sy-agent`, enrollment, mTLS, bundles, status, watchdog (M3), Go unit tests | done | | |
| Routing profiles: VRRP, BGP, BFD, static routes, MD5 ref | done | | |
| BGP prefix limits (01 routing profile) | missing | *not started*: no model field, no `maximum-prefix` in `frr.conf.j2` | Model field, template line, golden case |
| Status polling, SSE stream, event catalog, drift detection | done | Drift is reported, never fixed (by design) | |
| Proxmox adapter, SDN apply, IPAM, SE group deploy, retire (M4) | done | | |
| Engine host online state | missing | *no API*: A6 notes it is not exposed | Proxmox node-status call in the adapter and a field on `GET /engines` |
| Proxmox host layout and capacity (Placement) | missing | *no API*; engines only carry `pve_node` and `vmid` | Adapter call for nodes and storage, read endpoint |
| Certificates: import, PKCS#12 (ADR 0043), replace (ADR 0044), ACME DNS-01 (incl. step-ca), Origin CA, renew job (M5) | done | | |
| OCSP stapling, cipher and TLS-version profiles, mTLS to client and backend | missing | *not started*: in the 01 mapping, no model field or template output | Model fields, template, golden tests, ADR |
| Cloudflare-only allow list sync job | done | | |
| Cloudflare DNS record created from a virtual service | partial | Adapter has DNS calls; no service flow uses them (01 phase 2) | A job or change-set hook plus a model flag |
| Login: OIDC, SAML, break-glass, sessions, refresh (T1, G1-2, G1-4, G1-7) | done | | |
| Permission matrix, `/permissions`, 403 `code` (G1-1) | done | | |
| Identity providers, test sign-in, role mappings, users (G1-3, 5, 6) | done | | |
| Audit log filters, cursor, `/audit-actions` (A1) | done | | |
| Audit events to syslog | partial | JSON line from logger `switchyard.audit`; no syslog handler in `config.py` or `runtime.py` | Handler option and a docs line |
| Tenant quotas | partial | Stored and editable by system-admin; nothing enforces them when objects are created | Enforce in the validator with a problem code; counts query for the UI |
| Search, summary, VIP allocations, next-free, monitor test, engines list (A2 to A6) | done | | |
| UDP via IPVS, `udp` monitor, field rules, schema endpoint (M6, T6, T7) | done | | |
| Metrics and logs endpoints (A9) | partial | API done and answers `available: false`; the pipeline (HAProxy exporter, Prometheus, Vector, Loki) is not built, `HttpTelemetry` never run against a real stack | Deploy and document the pipeline; test against it |
| Prometheus HTTP service-discovery endpoint for SEs | missing | *not started* (02 telemetry) | Endpoint with `tenant`, `se_group`, `vs` labels |
| Controller own `/metrics` | missing | *not started*; no route in `app.py` or OpenAPI | `prometheus_client`: job durations, apply failures, drift count |
| Grafana embed or proxy (01, 03) | partial | VS Overview has a text hint only; no proxy | Proxy or link (*design question*) |
| `waf_policy`, Coraza SPOA | missing | *deferred*: phase 2 | Table, renderer, agent bundle, UI |
| Rate limits, IP groups | missing | *deferred*: phase 2 | |
| CrowdSec, oauth2-proxy SSO, caching, compression, Lua | missing | *deferred* or out of scope in 01 | |
| Passive checks (`observe layer7`), `external-check`, slow-start | missing | *not started*: in the 01 mapping, not in the model | Model fields and render rules |
| Pool discovery by Proxmox tags | missing | *deferred*: phase 2 | |
| Kubernetes connector | missing | *deferred*: phase 2 | |
| Avi config importer | missing | *deferred*: phase 2 | |
| Health score | missing | *deferred*: phase 2 (summary has counts only) | |
| SE scale-out (automatic) | missing | *deferred*: v3; manual deploy and retire exist | |
| GSLB, PowerDNS | missing | *deferred*: phase 3 | |
| Packet capture job | missing | *deferred*: phase 3 | |
| Tenant self-service portal | missing | *deferred*: phase 3 | |

## 2. Operations and deployment (02 "HA and deployment", "Repo layout")

| Item | Status | Why / note | What closing it takes |
| --- | --- | --- | --- |
| Dev Compose (PostgreSQL 17) | done | `deploy/dev/` | |
| Production Compose, Patroni, etcd, pgBackRest | missing | *not started*: only `deploy/dev` exists | Compose files, Patroni config, restore drill |
| Controller container image (`api` and `worker` entrypoints) | missing | *not started*: no Dockerfile except the test image `controller/tests/images/se-tools` | Dockerfile and release job |
| `se-image/` (cloud-init, OpenTofu, packages) | missing | *not started*; deploy clones a template by `template_vmid` | Template recipe and a runbook |
| Management tenant lock, `adopt` import, OpenTofu bootstrap | missing | *not started* | ADR, pipeline rule, import command |
| External (ESXi) controller member shown in UI | missing | *not started*; depends on adopt | Adopt design first |
| `docs/runbooks/` (restore DB, rotate CA, replace SE, emergency rollback) | missing | *not started*; the folder is absent | Four runbooks |
| CI | partial | `.gitlab-ci.yml` paused ("while we build and test locally"); no UI lane | Re-enable; add `ui-check` and axe |
| End-to-end lab test on Proxmox | missing | *not started* | Lab cluster plus a CI lane |

## 3. Frontend: screens (03, 04, handoff)

Every route exists; only unknown paths render the `NotBuilt` placeholder.

| Screen | Status | Gap | Why | What closing it takes |
| --- | --- | --- | --- | --- |
| Shell, tenant switcher, command palette, user menu, theme, stream indicator | done | | | |
| Login, four error states, break-glass | done | | | |
| Session-expired re-login modal | partial | Handler is registered in `api/client.ts`; whether the modal keeps form input was not verified | *not verified* | Check and add a test |
| Tenant home | partial | No "last check" column on failing members | *no API* | `last_checked_at` on the `summary` members |
| VS list | partial | Label filter is a disabled control ("Labels aren't supported yet"), as designed; RPS and 5xx show the unavailable state | *no API* (labels), *no pipeline* (metrics) | See section 1 |
| VS Overview | partial | No per-member health; metrics and logs unavailable | *no API* (members only on engine status), *no pipeline* | Members per service on `status/virtual-services` |
| VS Settings and History | done | | | |
| VS Rendered config | partial | Plain view; no Monaco, no range highlight | *deferred* in the build | Read-only Monaco |
| Create wizard (HTTP, TCP, UDP) | done | | | |
| Review and apply: Objects, Config, Impact | partial | Config diff is plain text, not Monaco | as above | Monaco diff editor |
| Approval, apply progress | done | | | |
| Conflict resolver | partial | Built and unit-tested; never run live against a controller | *not started* (live check) | Provoke a 412 against a real stack |
| Change sets list, Jobs | done | | | |
| Revisions: compare, rollback | partial | Revision "message" is read-only; apply takes no message | *no API* | `message` on apply and on the revision |
| Pools, health monitors (with test), policies | done | | | |
| Certificates: PEM, PKCS#12, issue, renew, replace | partial | `.p12` upload never run live | *not started* (live check) | Upload a real `.p12` against OpenBao |
| Networks, VIP pools, allocations, apply SDN | done | | | |
| SE groups: engines, deploy, register, enrollment, retire | partial | No Placement card; routing mode and networks read-only in group settings; "Host state not reported" | *no API* (host layout and state) | Host API (section 1), then the edit forms |
| Routing profiles | done | | | |
| Tenant access and settings | partial | Quota limits only, no usage counts | *no API* | Counts per object type on the tenant read |
| Admin: tenants, users, identity providers, break-glass, mappings, system tokens | done | | | |
| Admin role matrix | partial | Lists every server permission, not the artboard's eight grouped rows | *design question* | Group in the UI or add a `group` field to `/permissions` |
| Admin audit log | partial | Export covers loaded events only | *UI to wire* (export endpoint done, ADR 0060) | Use `GET /audit-events/export` |
| Drift: "Re-apply revision #N" offer | missing | Drift is shown; the one explicit action is not | *not started* | Button opening a rollback draft to the live revision |
| Handoff components (ShowOnceSecret, LiveBadge, IpUsageBar, BgpSessionChip, VrrpStateChip, WizardStepper, ApprovalBanner, MemberStateList, JobProgress) | partial | Built inside features, not as shared `@switchyard` components; only ChipInput, StatusPill, states, RenderedConfigView, ConfirmDialog are shared (JobProgress is in `features/review`) | *not started* | Extract when reused |
| Viewer (read-only) states | partial | Tests only; no viewer user in the demo data | *not started* | Seed a viewer in `syctl seed --example` |

## 4. Frontend quality bars (03, handoff "Not checked")

| Item | Status | Note |
| --- | --- | --- |
| Unit and component tests (Vitest, MSW) | done | 23 test files; 80% floor on `src/lib` and `src/auth` |
| axe in CI | missing | Not in `package.json` or CI |
| Playwright end-to-end | missing | Not in `package.json` |
| Phone widths, real data volumes, keyboard flows | missing | Design listed them as not checked; no one has since |
| Contrast 4.5:1 measured | missing | Designed, not measured |
| Live checks against a real controller (`.p12`, conflict) | missing | Mocked only |

## 5. Known gaps from the brief, verified

| Gap | Verified | Evidence |
| --- | --- | --- |
| Metrics and logs unavailable | yes | `adapters/telemetry.py`; `UnavailableState` in `VsOverview.tsx` |
| Proxmox host online state | yes | A6 note in 05; no field in `api/schemas.py` |
| Label filters | yes | Disabled control in `VirtualServicesPage.tsx` |
| SE group Placement card | yes | `docs/ui/README.md` known differences |
| Quota usage counts | yes | Same; the backend also does not enforce quotas |
| Role matrix grouping | yes | Same |
| Read-only revision message | yes | Same |
| Plain-text config diff | yes | Same; `RenderedConfigView.tsx` header says Monaco comes later |
| No per-member health | yes | Same |
| `.p12` upload and conflict view never run live | not verifiable from the repo | From the brief; the repo has mocked tests only |

## 6. Suggested order

1. Small backend additions: host state, members per service, `last_checked_at`, usage counts, revision message, quota enforcement.
2. Telemetry pipeline, so metrics and logs stop being unavailable.
3. Monaco for rendered config and diff; Re-apply revision #N; role matrix grouping.
4. Operations: controller image, production Compose and Patroni, SE image recipe, runbooks, CI back on with axe.
5. Live checks of `.p12` and conflict.
6. Phase 2 and 3 as the plan schedules them.

# Planned vs built, 2026-10-12

Audit of `origin/main` at d6002d0 (Merge branch 'k8s-deploy') against everything the repository says is planned: design docs 01
to 09, the UI handoff notes (`docs/design/ui-handoff/README.md`, `07-ui-vs-artboards.md` through "Design v53 answers"), all 120
files in `docs/adr/`, the parity plan and work blocks (08, 09), the ticket lists (05), the READMEs, `docs/production.md` and the
three lab reports in `docs/lab/`. This replaces [06-planned-vs-built.md](../design/06-planned-vs-built.md) (2026-10-10), which is out
of date.

## How to read this

Status words:

- **built+tested**: the code is on `main` and has automated tests. Where the item touches real infrastructure (engines, Proxmox,
  BGP, an external peer) a lab run is also on record in `docs/lab/` or `docs/production.md` section 12.
- **built, not lab-tested**: code and automated tests exist, but the item touches real infrastructure and no lab report or
  proven-list entry says it was run there (or the report says "not run"). Container integration tests (testcontainers with real
  HAProxy, PostgreSQL, PowerDNS and so on) are not counted as lab runs.
- **partial**: built with a stated difference from the plan.
- **missing**: not built.

Evidence is `file:line` for the implementation, then the test files (a test file name alone means it covers the whole item;
I did not map individual test functions). `golden/NN` means `controller/tests/golden/NN-*/`, each validated with `haproxy -c` by
`controller/tests/integration/test_golden_haproxy.py`.

**Limits of this audit.** It is static: I read code, tests, docs and the lab reports. I did not run the test suites, the UI or
the lab, so "tested" means "a test exists and the docs say it passes", not that I saw it pass. Lab results are copied from
`docs/lab/*` and `docs/production.md`. The UI handoff v53 files (`design-source/`, screenshots) are not in Git (the folder holds
only a README), so UI status rests on `07-ui-vs-artboards.md` plus a spot check of the code; no pixel comparison was done.

## Summary

Counts are over the 142 rows in sections 1 to 8 (some rows group several plan items). The three lists below are the item-level
view and are longer than the counts because they split grouped rows.

| Status | Rows |
| --- | --- |
| built+tested | 112 |
| built, not lab-tested | 13 |
| partial | 10 |
| missing | 7 |

### Missing (nothing built)

| Item | Where planned | Note |
| --- | --- | --- |
| Cloudflare DNS record created from a virtual service | 01 phase 2; 06 | Adapter exists; no service or job flow calls it |
| Automatic SE scale-out | 01 risks ("v3"); 08 C9 | Manual deploy, add-engine and a documented rolling replace exist; nothing scales by itself |
| Packet capture job | 01 phase 3; 08 D5 | |
| Tenant self-service portal | 01 phase 3; 08 D8 | |
| Multi-site and remote engines | ADR 0180 (Proposed) | |
| CrowdSec / bouncer | 01 mapping; 08 C11 | Reputation feeds are supported through `SWITCHYARD_REPUTATION_FEEDS`; no CrowdSec integration |
| Lua actions | 01 DataScripts; 08 D6 | Out of scope on purpose (ADR 0130) |
| `external-check` health scripts | 01 mapping | Refused on purpose (ADR 0066) |
| IPVS direct-return / tunnel modes | 08 D7 | Only NAT is rendered (`render/routing.py:18`, `:179`) |
| RADIUS login provider | 08 E5 | `radius` is rejected as a connector type (`controller/tests/unit/render/test_broker.py:152`) |
| Step-up / conditional flows (nFactor style) | 08 E8 | Device-posture conditions exist (ADR 0138); ordered multi-step flows do not |
| ZTNA gateway, agent-based device posture | 08 E9; ADR 0138 | Stated "Not built" in ADR 0138 line 31 |
| WAF API-schema protection | 08 D5 | |
| Object labels and the `?label=` filter | 04 / 05 A7 | Dropped by decision; the UI control is disabled (`ui/src/features/vs/VirtualServicesPage.tsx:154`) |
| External (ESXi) controller member shown in the UI | 06 section 2 | Adopt supports it (ADR 0051 line 26); nothing in `ui/src` mentions it |

### Partial

| Item | Gap |
| --- | --- |
| End-to-end lab test in CI | The lab scripts exist (`deploy/lab/e2e/`) but no CI lane runs them (`.gitlab-ci.yml` has check, deploy, helm, docs, agent, se-image only) |
| CI for the UI, Playwright, axe and the OpenTofu provider | No UI or provider job in `.gitlab-ci.yml`; `make ui-check` and `make ui-e2e` are local only |
| Coverage gate | `--fail-under=85` is on `domain`, `render`, `pipeline` only (`.gitlab-ci.yml:88`); correct per CLAUDE.md but the UI floor (80%) is not enforced in CI |
| Shared `@switchyard` shadcn registry; RHF, Zod, TanStack Table, Sonner | Not adopted (`ui/package.json`); selects are native, 8 hand-written files in `ui/src/components/ui/`; no ADR records the deviation (07 asks for one) |
| Directory-login policy editor in the UI | The list shows it read-only (`ui/src/features/policies/Policies.tsx:696`); the editor is not built, design is needed first (07) |
| Controller "use the default address" | API accepts `reset_to_default`; the screen always sends `false` (`ui/src/features/admin/AdminController.tsx:140`) |
| Viewer (read-only) states | Tests only; `syctl seed --example` has no viewer user |
| Kubernetes connector | Gateway `status.addresses` and conditions are not written, so `kubectl wait` and cert-manager's Gateway solver never see it ready (`docs/lab/2026-10-12-kubernetes.md`); deletion, drift and token rotation not run |
| Bot and DDoS: CAPTCHA | Built (ADR 0172) but never run against hCaptcha or live keys |
| Deny lists (country and reputation) | Built (ADR 0147); lab "not run", needs external feeds |
| GSLB `geo`, secondary AXFR, second site | `geo` needs a GeoIP database and was not checked; AXFR not run; the lab has one PowerDNS |
| GSLB UI | A screen exists (07 "Kubernetes and GSLB") without an artboard; per-member health is not in the API (ADR 0163) |
| TLS-ALPN-01 | Lab **fail**: first attempt returns `already exists` on the engine (lab findings 1) |
| Controller settings, managed mode | Management-tenant mode not run in the lab; no API path back to the default address once an FQDN is set (lab finding 3) |
| Helm / CloudNativePG | Install proven on k3s; database failover, `LoadBalancer`/Ingress and schema-changing upgrades not proven (`docs/lab/2026-10-12-kubernetes-install.md`) |

### Built but not lab-tested

Highest risk first (touches the data path or a real peer and has only container tests):

1. Backend client certificates (mTLS to members) and client-certificate identity (ADR 0107, 0136); `device_certificate` posture.
2. JWT validation, LDAP/SAML/local directory logins through the broker, groups and email-domain rules (lab: "not run").
3. OpenTofu provider with a real `tofu` binary.
4. Avi importer against a real export (tests use synthetic exports; the real backup cannot be committed).
5. BGP prefix limits, BFD to a real upstream in the two-host lab (BFD proven in the Docker lab only), BGP/IPVS in unprivileged containers.
6. WAF CRS catalogue suggestions, API-level disabled rules, HTTP/3 beyond one client, gRPC streams (h2c only).
7. JavaScript challenge solved by a browser, CAPTCHA, deny lists.
8. dnsdist in front of PowerDNS (container stack only).
9. Alert delivery to email and chat targets (lab: webhook only).
10. The whole UI against the lab (lab: "not run", built-in browser denied).

## 1. Backend core (02, 05, ADRs 0001 to 0018, 0030 to 0032, 0040 to 0047)

| Item | Status | Evidence | Note |
| --- | --- | --- | --- |
| Domain models, validators, layering (api → services → domain) | built+tested | `domain/models.py`, `domain/validators.py`; ADR 0001 | `tests/unit/domain/` (21 test files) |
| PostgreSQL schema, Alembic, RLS forced on every tenant table | built+tested | `db/migrations/versions/` (32 revisions); `tests/integration/test_rls.py:61` | Second enforcement in `services/` per ADR 0002 |
| Tenancy enforced twice, admin DB role separate | built+tested | `db/tenancy.py`, `domain/query_scope.py`; `test_rls.py`, `test_store.py` | |
| HAProxy renderer: deterministic, escaped, golden cases | built+tested | `render/haproxy.py`, `render/escape.py`; 33 golden cases; `test_golden_haproxy.py` | `haproxy -c` against real binary |
| Per-object `/rendered` fragments | built+tested | `render/fragments.py`; `tests/golden_fragments/`; `api/rendered_models.py` | |
| Change sets: ops, draft reads, diff, impact, warnings, 412/428 | built+tested | `services/changesets.py`, `domain/changes.py`, `domain/impact.py`; `test_changeset_service.py`, `test_api.py` | Live check via `deploy/lab/checks/live.sh` |
| Apply pipeline: lock, render, validate, canary, rollout, commit | built+tested | `pipeline/apply.py:94`, `pipeline/canary.py:32`, `pipeline/locks.py:23`; `test_apply_pipeline.py`, `test_e2e_real_engines.py` | Canary-first apply proven in the Docker lab |
| Revisions, rollback, Git mirror job | built+tested | `services/changesets.py:665`, `adapters/git.py`, `jobs/handlers.py:1`; `unit/test_git_mirror.py` | |
| Optional four-eyes approval, break-glass bypass, audit | built+tested | `services/changesets.py:561`; `db/audit_log.py`; `unit/test_audit_catalog.py` | |
| `?apply=true` single-op change set; revision `message` | built+tested | ADR 0046; `ui/src/features/review/Review.tsx:340` | |
| Typed API (T2), policy and routing unions (T4), OpenAPI completeness (A11) | built+tested | `api/schemas.py`; `docs/api/openapi.json` (161 paths); `unit/test_openapi.py`, `test_api_docs.py`; `openapi-breaking` CI job | |
| Filters, sort, cursor (A7) | partial | `?label=` dropped | See missing table |
| Audit log filters, cursor, export (A1) | built+tested | `api/app.py:5405`; `unit/test_audit_catalog.py` | CSV formula injection fixed (backend-review F4) |
| Audit events to syslog | built+tested | `runtime.py:328` | |
| Search, summary, VIP allocations, next-free, monitor test (A2 to A6) | built+tested | `api/app.py:2695`; `api/overview.py`; `test_api.py` | |
| Jobs: list, stages, log, retry, cancel, SSE (T5, A10) | built+tested | `jobs/queue.py`, `jobs/worker.py`; `test_queue.py`, `test_jobs.py`, `test_worker_flow.py` | Queue in PostgreSQL, no Redis |
| Scheduler leader (advisory lock) | built+tested | `jobs/scheduler.py`; Helm lab run: exactly one lock held | |
| Housekeeping / garbage collection | built+tested | `services/gc.py:39`; `test_gc.py` | |
| Tenant quotas enforced (`quota_exceeded`), usage on tenant read | built+tested | `domain/changes.py:346-381`; ADR 0045 | |
| Login: OIDC, SAML, sessions, refresh, break-glass, throttling | built+tested | `services/oidc.py:26`, `services/saml.py:102`, `services/sessions.py:67`, `services/break_glass.py:36`, `services/throttle.py:29`; `test_login.py`, `test_saml_login.py` | Dex OIDC proven in the lab |
| Role matrix, `/permissions`, 403 `code`; IdP admin, role mappings, users | built+tested | `domain/permissions.py:24`; `test_permission_matrix.py`, `test_idp_admin.py`, `test_access_admin.py` | |
| Principal display names | built+tested | `services/principals.py`; `test_principals.py` | |
| API tokens (tenant and system) | built+tested | `api/app.py` token routes; `test_api.py` | |
| `syctl` CLI and `syctl api` client | built+tested | `cli.py`, `client.py`; `unit/test_cli.py`, `test_syctl_api.py` | |

## 2. Data plane: HAProxy features (08 phases A and C, ADRs 0100 to 0119)

| Item | Status | Evidence | Note |
| --- | --- | --- | --- |
| A1 Cookie, header and URI persistence; source IP | built+tested | `domain/models.py:794`; golden/16-persistence-and-member-options; `test_haproxy_behavior.py` | Lab: cookie sticky, source IP with peers: pass |
| A2 Header rewrite and URL rewrite | built+tested | `domain/models.py:1348`; golden/17-rewrite-policies | Lab: pass (branch) |
| A3 Route matches: method, header, query, CIDR, SNI | built+tested | `domain/models.py:446`; golden/16-route-match-model | |
| A4 Fixed responses, custom error pages | built+tested | `domain/models.py:1392`, `:1407`; golden/18-responses-and-error-pages | Lab: respond 200/403 pass; `error_pages` and Retry-After not run |
| A5 Rate and connection limits | built+tested | `domain/models.py:1460`; golden/19-rate-limits | Lab: pass |
| A6 Backup members, per-member maxconn, timed drain, send-proxy | built+tested | `domain/models.py:919`, `:983`, `:100`; golden/16-persistence-and-member-options | Lab: backup, timed drain pass |
| A7 Backend mTLS | built, not lab-tested | `domain/models.py:984`; golden/17-health-presets-backend-mtls | Not in the lab report |
| A8 HTTPS body/header match, TCP presets (Redis, SMTP, LDAP, MySQL) | built, not lab-tested | `domain/models.py:1072`; golden/17 | Presets only; no `external-check` (by decision) |
| Passive checks, slow start | built, not lab-tested | `domain/models.py:974-975`; golden/13 | |
| C1 HTTP/3, gRPC, WebSocket options | built+tested | `domain/models.py:611`; golden/18-passthrough-http3-compression | Lab: real QUIC client pass; h2c pass, gRPC streams not run |
| C2 Weighted pool routes, priority groups | built+tested | `render/haproxy.py:1444`, `domain/models.py:526`; golden/20, 21 | Lab: 70/30 split pass; priority groups not named in the lab |
| C3 Persistence `peers` | built+tested | `render/haproxy.py:73`; golden/19-peers-sync | Lab: stick tables shared: pass |
| C4 TLS passthrough with SNI routing | built+tested | `render/haproxy.py:796` | Lab: pass |
| C5 Compression; response cache | built+tested | `render/haproxy.py:782`; golden/21-failover-and-cache | Lab: gzip pass; cache not run |
| Client mTLS (verify, CRL), TLS profiles, OCSP stapling | built, not lab-tested | `domain/models.py:590-592`, `:1126`; golden/14, 15 | |
| IPv4 and IPv6 / dual-stack rendering | built+tested | golden/10-ipv6; ADR 0078 | Lab: public IPv6 VIPs pass |
| Cloudflare-only allow list, real client IP | built+tested | `jobs/handlers.py:204`; golden/07 | |
| TCP L4 multiport, extra ports | built+tested | golden/06, 08 | |
| UDP via Keepalived/IPVS, `udp` monitor, field rules | built+tested | `domain/field_rules.py:65`, `agent/internal/ipvs/ipvs.go:58`; golden/09, golden_routing/04-05 | Lab: v4 and v6 pass |
| Exporter listener, management network | built+tested | `render/haproxy.py:65`; golden/12 | |

## 3. Routing, agent and engines (02, ADRs 0007 to 0011, 0041, 0065, 0074, 0079)

| Item | Status | Evidence | Note |
| --- | --- | --- | --- |
| `sy-agent`: enrollment, mTLS, bundles, status, allowlisted reloads | built+tested | `agent/internal/*/`; Go tests in `agent/internal/*/*_test.go` (8 files) and `agent/cmd/sy-agent/main_test.go` | |
| Bundle binding to engine and rising sequence | built+tested | `pipeline/bundles.py`; ADR 0074; `test_agent_contract.py` | |
| Route-health watchdog (BGP withdraw) | built+tested | `agent/internal/watchdog/watchdog.go:46`; `test_pipeline_routing.py` | Lab: ECMP over 3 engines, one stopped removes a path |
| VRRP (Keepalived), failover | built+tested | `render/routing.py:297`; golden_routing/01 | Lab: 4 to 5 s cross-host |
| BGP, ECMP, BFD, static routes | built+tested | `render/routing.py:337`, `:375`; golden_routing/02-03 | Lab: ECMP pass; BFD to upstream in the Docker lab only |
| BGP prefix limits | built, not lab-tested | `domain/models.py:315`; golden_routing/06; ADR 0065 | |
| Withdrawn prefixes, member probe | built+tested | ADR 0041; `agent/internal/probe/` | v6 withdrawal fixed in !100 |
| Engine enrollment, tokens, registration, retire | built+tested | `services/enrollment.py:37`, `services/engines.py:36`; `test_enrollment.py` | |
| Engine registration restrictions (SSRF, https, CIDR) | built+tested | `services/hosts.py:33`; `docs/security/backend-review.md` O1; `test_hosts.py` | |
| Status polling, SSE, drift detection (reported, never auto-fixed) | built+tested | `services/status.py:62`; `api/app.py:5107`; `test_status.py` | |
| Drift re-apply of the desired state | built+tested | `api/app.py:3079`; ADR 0069 | Lab: rolling replace used it |
| Running config of an engine (hashes only for agent files) | built+tested | `api/app.py:3056` | |
| Health score for SE groups and engines | built+tested | `services/health.py:43`; `test_health.py` | Lab: pass incl. WAF sidecar down |
| Controller-less operation (SEs keep serving) | built+tested | `docs/production.md` section 12 | Controller-less reboot proven in the Docker lab |
| Canary on every apply | built+tested | `pipeline/canary.py:32` | |

## 4. Infrastructure: Proxmox, IPAM, SDN, certificates (M4, M5, ADRs 0009, 0010, 0016, 0017, 0043, 0044, 0049, 0055, 0057, 0080, 0082)

| Item | Status | Evidence | Note |
| --- | --- | --- | --- |
| Proxmox adapter, host state, placement, capacity | built+tested | `adapters/proxmox.py:21`; `test_proxmox_live.py`, `unit/test_proxmox_http.py` | Lab: both hosts |
| SE group deploy (VM and LXC), retire, data-NIC addressing | built+tested | `services/infra.py:54`; `test_infra.py` | Lab: VM, LXC and unprivileged LXC; BGP/IPVS in unprivileged LXC not tried |
| SDN apply, networks, VIP pools, IPAM | built+tested | `domain/ipam.py:55`; `test_infra.py`, `unit/domain/test_ipam.py` | |
| Certificates: import, PKCS#12, replace in place | built+tested | `services/certificates.py:56`; ADR 0043, 0044; `test_certificates.py` | Lab: `live.sh` pass; HTTPS service pass |
| Cloudflare Origin CA, DNS-01, renew job | built+tested | `adapters/cloudflare.py:19`, `adapters/acme.py:46`; `test_acme_providers.py`, `unit/test_cloudflare_http.py` | |
| ACME providers (several CAs, EAB, OAuth), DNS credentials | built+tested | `services/acme_providers.py`, `services/dns_providers.py`; ADR 0080 | |
| ACME HTTP-01 | built+tested | `services/http01.py:27`; `test_http01.py`, `test_acme_pebble.py` | Lab: Pebble pass |
| ACME TLS-ALPN-01 | partial | `services/tlsalpn.py:37`; `unit/test_acme_alpn.py` | Lab **fail** |
| Certificate renew-before, expiry notifications | built+tested | `services/alerts.py`; ADR 0064, 0122 | Lab: cert-expiry rule fires (branch) |
| Controller FQDN, VIP and certificate from the UI | built+tested | `services/controller_settings.py:62`; `test_controller_settings.py` | Lab: unmanaged mode only |
| Management tenant lock, `adopt` import | built+tested | `domain/management.py:23`, `services/adopt.py:69`; `test_cli_db.py` | |
| Pool discovery by Proxmox tag | built+tested | `services/discovery.py:60`; `test_pool_discovery.py` | Lab: pass (VM and container) |
| Secrets: OpenBao references only | built+tested | `adapters/secrets.py:21`; ADR 0071; `unit/test_openbao.py` | |
| Rolling SE replace, health-gated | partial | `docs/runbooks/rolling-upgrade.md`; ADR 0083 | It is a documented procedure against the health-score gate, not a job; lab pass |
| Add an engine to a group (manual scale-out) | built+tested | Same ADR 0083 | Lab: pass |

## 5. Platform integrations and phase 2/3 features (08 phases C, D, E)

| Item | Status | Evidence | Note |
| --- | --- | --- | --- |
| Kubernetes connector (D2) | partial | `connectors/kubernetes.py:37`; `test_k8s_connector.py`; ADR 0151 | Lab pass for sync and VIP write-back; Gateway status gap |
| Avi importer (D3) | built, not lab-tested | `importers/avi.py:135`; `test_avi_import_service.py` | Not run on the real export |
| OpenTofu provider (C10) | built, not lab-tested | `provider/`; ADR 0146 | Lab: "no binary approved" |
| GSLB on PowerDNS (D1): zones, wide IPs, DNSSEC | built+tested | `domain/gslb.py:45`, `services/gslb.py:550`; `test_gslb.py`, `test_gslb_stack.py` | Lab: failover, all, weighted, subnet, DNSSEC pass |
| GSLB geo / latency / dnsdist | built, not lab-tested | ADR 0164, 0165; `deploy/dns/dnsdist.conf`; `test_dnsdist_stack.py` | |
| Telemetry: exporter, Prometheus, Vector, Loki, Grafana | built+tested | `adapters/telemetry.py:210`, `deploy/telemetry/`; `test_grafana_stack.py`, `unit/test_telemetry.py` | Lab: 14 targets, 27 panels |
| Per-tenant Grafana (one org per tenant) | built+tested | `services/grafana.py:130`, `api/grafana_proxy.py`; `test_grafana.py` | Lab (branch): pass |
| Request logs API (B3) and metrics endpoints | built+tested | `api/app.py` (`/logs`, `/metrics`); `test_observability.py` | Lab: pass |
| Prometheus service discovery, controller `/metrics` | built+tested | `api/app.py:1421`; ADR 0061 | |
| Alerting: targets, rules, evaluator | built+tested | `services/alerts.py:510`, `adapters/notify.py:32`; `test_alerts.py` | Lab: webhook and HMAC only |
| WAF (Coraza SPOA + CRS) | built+tested | `domain/models.py:1474`, `render/waf.py:34`; golden/22-waf | Lab: detect, block, fail modes pass |
| Bot and DDoS protection | built+tested | `domain/models.py:1956`; golden/23 | Lab: bot rules pass |
| JavaScript challenge | built, not lab-tested | `render/captcha.py:161`; golden/24; `test_js_challenge.py` | Lab: challenge page served; proof of work not solved |
| CAPTCHA gate | partial | golden/25; `test_captcha_gate.py` | |
| Country and reputation lists | partial | `services/iplists.py:48`, `jobs/handlers.py:80`; `test_ip_list_sync.py` | |
| Login in front of services (OIDC) | built+tested | `domain/models.py:1678`; golden/20; ADR 0130 to 0133 | Lab: Dex pass |
| Identity headers, auth events | built+tested | `render/auth.py`; ADR 0132, 0133 | Lab: session cookie not forwarded |
| JWT validation and JWKS sync | built, not lab-tested | `domain/models.py:1779`; golden/21; `test_jwks_sync.py` | |
| Client-certificate identity | built, not lab-tested | `domain/models.py:1809`; golden/22-client-cert-identity | |
| LDAP / SAML / local directory logins (Dex broker) | built, not lab-tested | `domain/models.py:2107`, `render/broker.py:58`; `test_broker_dex.py` | |
| Device posture claims | built+tested | ADR 0138; golden/20 | Lab: `present`/`equals` pass; `device_certificate` not run |
| RADIUS; nFactor flows; ZTNA | missing | see Missing table | |
| Lua, CrowdSec, `external-check` | missing | | By decision |

## 6. Operations and deployment (06 section 2, ADRs 0048 to 0056, 0075, 0077, 0083, 0191)

| Item | Status | Evidence | Note |
| --- | --- | --- | --- |
| Dev Compose | built+tested | `deploy/dev/compose.yaml` | |
| Quickstart Compose with OpenBao, TLS front | built+tested | `deploy/quickstart/`; `docs/lab/2026-10-12-kubernetes-install.md` B | Privileged LXC proven incl. reboot |
| Production Compose, Patroni, etcd, pgBackRest | built, not lab-tested | `deploy/prod/compose.yaml`, `deploy/prod/patroni/`; `deploy/prod/lab-drill.sh` | Failover and restore rehearsed on one host; "multi-node production Compose end to end across hosts" listed as pilot item (`docs/production.md` §12) |
| Controller image | built+tested | `deploy/controller/Dockerfile` | |
| Nginx/UI proxy | built+tested | `deploy/proxy/` (`smoke.sh`) | |
| Helm chart | partial | `deploy/helm/switchyard/`; `deploy/helm/test.sh` | Lab k3s pass; DB failover and schema-changing upgrade not proven |
| SE image recipe (cloud-init, OpenTofu, packages, sidecar slots) | built+tested | `se-image/`; `se-image/test/check.sh` (CI `se-image` job); ADR 0050, 0083 | Lab: templates built, engines enrolled on two hosts; `coraza-spoa` and CRS in the image per `versions.env` |
| Runbooks (restore DB, rotate CA, replace SE, rollback, template, rolling, telemetry, broker, GSLB lab, lab hosts) | built+tested | `docs/runbooks/` (10 files); `deploy/prod/ca-rotation-drill.sh` | Restore and CA rotation rehearsed on the lab drill |
| End-to-end lab on Proxmox | built+tested | `deploy/lab/e2e/`, `docs/lab/2026-10-12-full-pass.md` | Manual; not in CI |
| Live checks (`.p12`, conflict) | built+tested | `deploy/lab/checks/live.sh` | Lab: pass |
| CI: lint, types, Docker-free tests, container smoke, full tier, docs, Go agent, SE image, Helm, OpenAPI breaking | built+tested | `.gitlab-ci.yml:30-156` | No UI, Playwright or provider job |
| Coverage ≥ 85% on domain, render, pipeline | built+tested | `.gitlab-ci.yml:88`, `controller/Makefile:25` | Runs on the full tier only |
| Security review (backend, UI) | built+tested | `docs/security/`; fixes in `test_security_fixes.py` | 6 low/info items left open |
| Docs site | built+tested | `mkdocs.yml`, ADR 0190 | |

## 7. Frontend (03, 04, 07, handoff v46 to v53)

All rows: Vitest/MSW tests in `ui/tests/` (44 files), Playwright axe/keyboard/phone checks in `ui/e2e/` (7 specs), 80% floor on
`src/lib` and `src/auth` (`ui/vite.config.ts:41`). The lab did not exercise the UI, so "built+tested" here means mocked-API
tests only.

| Screen or component | Status | Evidence | Note |
| --- | --- | --- | --- |
| Shell, tenant switcher, command palette, user menu, theme, stream indicator | built+tested | `ui/src/features/shell/`; `shell.test.tsx`, `stream.test.tsx` | SSE patches caches, stale banner (07 B6) |
| Login, error states, break-glass, session-expired modal | built+tested | `ui/src/features/login/`; `login.test.tsx`, `session-expired.test.tsx` | |
| Tenant home (alerts tile, last check) | built+tested | `ui/src/features/home/Home.tsx` | |
| VS list, Overview (metrics SVG panels), Settings, History, Rendered config (Monaco) | built+tested | `ui/src/features/vs/`; `vs-*.test.tsx`, `monaco.test.ts` | |
| Create wizard (HTTP, TCP, UDP) | built+tested | `ui/src/features/wizard/`; `wizard.test.tsx` | |
| Review and apply (Monaco diff, impact, approval, conflict, job progress, revision message) | built+tested | `ui/src/features/review/`; `review.test.tsx`, `conflict.test.tsx` | Conflict view never run live in a browser |
| Change sets, Jobs, Revisions | built+tested | `ui/src/features/changesets/`, `jobs/`, `revisions/` | |
| Pools (discovery, persistence, member options), monitors (with test) | built+tested | `ui/src/features/pools/`, `monitors/`; `pools-monitors.test.tsx` | |
| Policies: rewrite, fixed, rate limit, WAF, bot, access, JWT, client cert | built+tested | `ui/src/features/policies/`; `policies-layout.spec.ts` | No artboards; Design to confirm |
| Policies: directory (LDAP/SAML/local) editor | partial | `Policies.tsx:696` | Read-only |
| Certificates (PEM, PKCS#12, issue, ACME request, renewals) | built+tested | `ui/src/features/certificates/`; `request-cert.test.tsx`, `acme.test.tsx` | |
| Networks, VIP pools, allocations, SDN | built+tested | `ui/src/features/networks/`; `networks.test.tsx` | |
| SE groups: engines, placement, deploy (VM/LXC), drift Re-apply, running config, engine metrics | built+tested | `ui/src/features/segroups/`, `drift/`; `se-groups.test.tsx` | |
| Routing profiles | built+tested | `ui/src/features/routing/`; `routing-profiles.test.tsx` | |
| Tenant access and settings (quota usage) | built+tested | `ui/src/features/access/Access.tsx:603` | |
| Alerts and Request logs (Observe) | built+tested | `ui/src/features/alerts/`, `logs/` | No artboards |
| Kubernetes and GSLB | built+tested | `ui/src/features/kubernetes/`, `gslb/`; `kubernetes.test.tsx`, `gslb.test.tsx` | No artboards |
| DNS credentials, ACME providers | built+tested | `ui/src/features/dns/`, `admin/AcmeProviders.tsx` | |
| Admin: tenants, users, IdPs, role mappings (grouped), system tokens, audit log (server export), controller, system settings | built+tested | `ui/src/features/admin/`; `admin.test.tsx`, `controller.test.tsx` | Controller "use default address" not offered |
| ShowOnceSecret, ConfirmDialog, ChipInput, StatusPill, states, TimeSeries | built+tested | `ui/src/components/sy/` | Other handoff components live inside their features |
| Viewer read-only states | partial | no viewer in `services/seed.py` | |
| shadcn registry, RHF/Zod, TanStack Table, Sonner | partial | `ui/package.json`, `ui/src/components/ui/` | Deviation not recorded in an ADR |
| IP lists, Avi import, OpenTofu: no UI | missing | `ui/src/features/` has no directory for them | Not in the handoff; API/CLI only (confirm intended) |
| External controller member in System/Controller | missing | no match in `ui/src` | |

## 8. Quality bars and documentation

| Item | Status | Evidence | Note |
| --- | --- | --- | --- |
| Every change tested; golden + `haproxy -c`; RLS isolation tests | built+tested | 33 golden cases; 55 integration, 30 top-level unit and 21 domain unit test files, plus `unit/render/` | |
| Gates (ruff, ruff format, mypy --strict) | built+tested | `.gitlab-ci.yml:30-42` | I did not run them |
| ADRs for decisions | built+tested | 120 files, all in `docs/adr/README.md`; one Proposed (0180) | Numbers 0019, 0021 to 0029, 0033 to 0039, 0059 and others are unused ranges, not lost files |
| Docs per feature; production guide with proven list | built+tested | `docs/features/` (28 pages), `docs/production.md` §12 | |
| Axe in CI, Playwright in CI | missing | `ui/package.json:39`; not in `.gitlab-ci.yml` | Runs locally (`make ui-e2e`) |
| Real-browser UI check against the lab | missing | `docs/lab/2026-10-12-full-pass.md` UI row | |
| License | missing | README: "License TBD" | |

## Stale statements found in the docs

| Doc | Statement | Reality | Action |
| --- | --- | --- | --- |
| `docs/design/06-planned-vs-built.md` | Whole file (2026-10-10) | Most "missing" rows are built (production deploy, SE image, runbooks, Kubernetes, Avi, GSLB, WAF, health score) | Banner added pointing here |
| `docs/ui/README.md` "Known differences" | Revision message read-only; plain-text diff; no per-member health; no Placement card; no usage counts; audit export loaded-only; role matrix ungrouped | All built (`Review.tsx:340`, `ConfigEditor`, `usePlacement`, `Access.tsx:603`, `AuditLog.tsx`) | Section replaced with the real differences |
| `docs/design/05-frontend-backend-tickets.md` A9 | "the telemetry pipeline is not built" | Built and lab-run | Left alone (historic ticket table); noted here |
| `docs/design/08-...` and `09-...` | "Planning only" | Almost all blocks merged | Left alone; 09's wave table is history |
| `docs/design/ui-handoff/` | Only a README in Git | `design-source/` and screenshots live outside Git | Noted; the v53 handoff cannot be compared from the repo |
| `docs/design/07-...` Summary | "Screens that match: 9 … differ 18" | Superseded by its own "Status after the build threads" and later sections | Left alone |

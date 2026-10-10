<!-- Snapshot exported 2026-10-10 from the online doc: https://claude.ai/code/artifact/bf06390f-6cf2-4fa9-9128-b6e8286ecb93
     The online doc may be newer. Names updated to Switchyard (syctl, sy-agent). -->

# NSX ALB (Avi) → Open-Source LB Replacement Plan

Oct 10, 2026 · @Miguel Rodriguez

## Summary

Replace Avi with multi-tenant HAProxy service-engine (SE) groups on Proxmox and a custom controller whose UI builds everything net-new: tenants, networks, VIP pools, routing, SE groups and services. Phase 1 is one small SE group sized for today's \~20 services, but the object model and UI are multi-tenant from day one, so growth means adding tenants and SE groups, not redesigning. Avi is really three products in one: a data plane (Service Engines), a control plane (Controller cluster), and an analytics stack. Each gets an open-source equivalent; the controller is the piece no single project provides, so we build it.

Key decisions:

- **Start small, build multi-tenant:** phase 1 is one tenant and one 2-VM SE group covering what Avi does today (see What the Avi backup shows). Tenancy, RBAC, per-tenant networks and multiple SE groups are in the data model and UI from the start; GSLB, autoscale and the Kubernetes connector come in later phases.
- **Data plane: HAProxy Community** on Debian VMs, with Coraza (OWASP CRS) for WAF via SPOE as a later phase, since Avi's WAF runs detection-only today and Cloudflare's WAF already sits in front. One engine covers L4, L7, TLS, persistence, rate limiting and Lua scripting (DataScript equivalent).
- **VIP placement: BGP route health injection** from FRR on each LB VM, peering eBGP to the upstream routers configured for that SE group in the UI (for example a tenant's FortiGate VDOM), ECMP for active/active. Keepalived VRRP is the other mode; the HA mode is chosen per SE group in the UI, and phase 1 can start on either. FRR runs on the LB VMs, never on PVE hosts, so it cannot collide with Proxmox SDN's FRR.
- **GSLB: PowerDNS, later phase** Authoritative with LUA health-checked records and dnsdist in front. Not in phase 1, since Avi has no GSLB today.
- **Observability: Prometheus (or VictoriaMetrics) + Loki + Grafana**, fed by HAProxy's built-in exporter and Vector for request logs.
- **Cloudflare origin hardening:** issue or import Origin CA certs from the UI, allow only Cloudflare IP ranges to the public frontend, restore client IPs from `CF-Connecting-IP`, and enable Authenticated Origin Pulls (mTLS). Avi does none of this today.
- **Kubernetes:** phase 1 uses in-cluster tools for LoadBalancer IPs and API VIPs (Cilium LB-IPAM or MetalLB, kube-vip) instead of an AKO equivalent. A controller-side Kubernetes connector that watches Services and Gateways and creates virtual services per tenant is a later phase.
- **UDP: Keepalived LVS (IPVS)** on the same SE VMs, since HAProxy Community does not proxy generic UDP. The UI exposes it as an L4 UDP virtual service type (WireGuard, DNS, syslog).
- **Controller: custom build** (API-first, CLI + web UI, Git-backed config) that drives the HAProxy Data Plane API, Keepalived/FRR, the Cloudflare API and the Proxmox API. Roxy-WI is the nearest off-the-shelf UI, but it is config-file centric, not object-model centric, and has an unpatched SSRF (CVE-2026-45561, versions 8.2.6.4 and earlier) as of publication, so use it at most as a lab stopgap.
- **Licensing guardrail:** avoid BSL-relicensed HashiCorp tools. Use OpenTofu (not Terraform) and OpenBao (not Vault); skip Consul and use Proxmox VM tags for discovery.

## What the Avi backup shows

Avi is used here as a Cloudflare-fronted SNI reverse proxy plus a few L4 services and AKO for one RKE2 cluster. The backup sets what phase 1 must support, not the network design: the new platform is net-new, so no current IPs, routes or VIPs carry over, and every service below is recreated through the UI on new networks. Source: controller backup of 2026-10-06, Avi 22.1.2.

- **Scale:** 21 virtual services on 9 VIPs in one /24 (atl-lb, 10.60.3.0/24), one tenant (admin), one SE group (4 vCPU / 8 GB, N+M, 4 SEs). Real use is the NSX-T cloud; the vCenter and Default clouds hold no services.
- **VIP placement:** NSX-T cloud with static routes on the Tier-1 (nsx-t1-gw). No BGP and no GSLB anywhere.
- **Main pattern:** one SNI parent (External-ParentSNI, 10.60.3.20) with 12 SNI children, each a hostname mapped to a usually single-server pool. The parent also listens on UniFi ports 80, 8880, 8843, 8075, 6789 and 8682.
- **Certificates:** every public cert is a **Cloudflare Origin CA** cert, mostly wildcards (corp-a.example, corp-b.example, home.example, lab-files.example, insure-a.example), valid to 2038–2039. The Let's Encrypt management profile exists but no cert uses it. The one AKO-issued Let's Encrypt cert (rancher.k8s.int.example.test) **expired 2026-08-03**.
- **Security features:** WAF is on 2 services in **detection-only, low paranoia** (CRS 2021-2). All 19 network security policies are empty and no bot policy is attached. The origin accepts traffic from any source, not only Cloudflare.
- **Policies and scripts:** no custom DataScripts in use; the only custom HTTP rules are 2 Nextcloud CardDAV/CalDAV redirects. Persistence is used once (client IP on WHMCS).
- **Health monitoring:** the 12 SNI children have no health monitor at all; the L4 pools use plain TCP checks.
- **Kubernetes:** AKO runs for k8s-prod-a: an EVH parent for Rancher (cattle-system) and an L4 LoadBalancer VIP for Envoy Gateway (10.60.3.23). Leftovers from an old legacy-production cluster remain: pools, L4 policies, a DataScript and pool groups with no virtual service.
- **Broken or odd:** the 51820-WGVPN service runs WireGuard (UDP) through a TCP proxy profile, so it cannot have carried WireGuard traffic. Web LB-pool points at 1.1.1.1.

### Migration inventory

| Avi virtual service | Type, VIP:ports | Backend | Target |
| --- | --- | --- | --- |
| External-ParentSNI | SNI parent, .20: 443, 80, UniFi ports; WAF detect | Ubiquiti 10.60.1.4:8080 (TLS) | HAProxy frontend, `crt-list` + SNI map |
| Keycloak SSO | SNI child: sso, sso-admin.corp-a.example | 10.60.1.5:8080 | HAProxy SNI backend |
| PWD Production Store | SNI child: pwdstore.prod.corp-a.example | 10.60.0.5:8096 | HAProxy SNI backend |
| Office Online | SNI child: oos.prod.corp-a.example | 10.60.6.3:80 | HAProxy SNI backend |
| IPAM | SNI child: ipam.prod.corp-a.example | 10.60.5.100:80 | HAProxy SNI backend |
| Rancher UI | SNI child: rancher.prod.corp-a.example | 10.60.0.5:10002 (TLS) | HAProxy SNI backend |
| insure-a.example | SNI child, WAF detect, full logs | 10.61.0.3:443 (TLS) | HAProxy SNI backend + Coraza later |
| Ubiquiti UISP | SNI child: uisp.corp-b.example | 10.60.1.4:8080 (TLS) | HAProxy SNI backend |
| billing.corp-b.example | SNI child, client-IP persistence | 10.60.1.2–.3:443 (TLS), 2 servers | HAProxy SNI backend, `stick on src` |
| dev headscale | SNI child: dev-hsendpoint.corp-b.example | 10.60.5.182:8080 | HAProxy SNI backend (WebSocket/HTTP2) |
| Files Nextcloud | SNI child: files.lab-files.example, 2 redirects | 10.60.1.7:11000 | HAProxy SNI backend + 2 redirect rules |
| lab-files.example + Matrix | SNI child: 3 hostnames | 10.61.0.3:443 (TLS) | HAProxy SNI backend |
| mealie.home.example | SNI child | 10.40.1.3:9925 | HAProxy SNI backend |
| K8S PRD CLUS 01 ×4 | L4, .27: 80, 443, 6443, 9345 | 10.60.6.27–.32 | HAProxy TCP, or kube-vip in-cluster |
| k8s-a Envoy Gateway (AKO) | L4 LoadBalancer, .23: 80 | NodePorts on 10.60.6.27–.32 | Cilium LB-IPAM or MetalLB in-cluster |
| k8s-a cattle-system EVH (AKO) | L7, .21: 80, 443 | Rancher ingress | In-cluster ingress; renew the expired cert |
| 51820-WGVPN | L4 TCP proxy, .22: 51820 | 10.60.0.5:51820 | L4 UDP service (IPVS) on the SE group |
| NSX Management | L7, .28: 443 | NSX managers | Retire with NSX |
| Unused EVH VIPs (wazuh, checkmate, default) and legacy-production objects | — | — | Do not migrate |

## Avi function → open-source replacement

Every Avi feature in common use has a mature open-source equivalent; the gaps are ML-driven analytics and automatic SE elasticity, which the controller approximates.

| Avi function | Replacement | Notes |
| --- | --- | --- |
| Service Engine (L4/L7 virtual services) | HAProxy Community | TCP/UDP/HTTP/1.1/2/3 (QUIC), content switching, redirects, header rewrites |
| Pools, server weights, LB algorithms | HAProxy backends | roundrobin, leastconn, source, uri, hdr, random; slow-start |
| Health monitors (HTTP, TCP, ping, external) | HAProxy checks + `external-check` | Passive checks via `observe layer7` |
| Persistence (cookie, source IP, header, TLS) | HAProxy stick tables, `cookie` | Peers section syncs tables across the SE pair |
| SSL/TLS termination, SNI, profiles | HAProxy `crt-list` | Per-SNI certs, cipher/TLS-version profiles, OCSP stapling, mTLS to client and backend |
| Certificate management, Let's Encrypt | lego or acme.sh + HAProxy Runtime API | Controller issues once via Cloudflare DNS-01 (wildcard or per-host), pushes to every SE node, hot-swaps without reload; internal PKI via smallstep step-ca or OpenBao PKI |
| WAF (CRS signatures, learning mode) | Coraza SPOA + OWASP CRS | Detection-only mode first, then blocking per app; no auto-learning, tune by log review |
| DataScripts (Lua) | HAProxy Lua | Same language, different API; rewrite each script |
| HTTP policies, rate limiting, DDoS | HAProxy ACLs, stick-table rate counters | Kernel SYN cookies + nftables for L3/L4 floods |
| IP reputation, bot protection | CrowdSec + HAProxy bouncer | Community blocklists, local detection scenarios |
| Caching, compression | HAProxy `cache` + `compression` | Varnish behind HAProxy if a heavy cache is needed |
| SSO at the LB (SAML/OIDC) | oauth2-proxy or Authentik forward-auth | HAProxy `http-request` auth subrequest |
| VIP HA, scale-out, BGP RHI | FRR (BGP, ECMP) or Keepalived (VRRP) | Withdraw route when HAProxy or its health fails |
| VRF contexts, tenant networks | Proxmox SDN VNets + multi-NIC LB VMs | Created per tenant from the controller UI |
| SE groups, autoscale, self-healing | Controller + Proxmox API + cloud-init template | OpenTofu bpg/proxmox provider for declarative SE VMs |
| Pool auto-population (vCenter cloud) | Controller connector on Proxmox API | Pool membership from VM tags, e.g. `lb-pool-web` |
| GSLB (health-based DNS, geo, failover) | PowerDNS LUA records (`ifurlup`, `pickclosest`) | dnsdist for rate limiting and query distribution |
| DNS virtual service, IPAM/DNS profiles | PowerDNS Authoritative API | Controller creates A records when a VS is created |
| Analytics, logs, client insights | Vector → Loki; HAProxy Prometheus exporter → Prometheus; Grafana | Per-request logs, latency breakdown (Tq/Tw/Tc/Tr/Tt), error codes |
| Health score | Controller-computed score | Weighted from check status, error rate, latency vs baseline, capacity |
| Alerts, events | Alertmanager + controller event log | Webhook to Teams/Slack/email |
| Packet capture | Controller job running `tcpdump` on the SE | pcap returned to the UI |
| Controller cluster (3 nodes) | Controller app ×2–3 + PostgreSQL (Patroni) | Config mirrored to Git for rollback |
| REST API, Terraform/Ansible | Controller OpenAPI + CLI | OpenTofu provider later if wanted |
| RBAC, tenants | Controller RBAC + OIDC (Authentik, Keycloak, or Entra ID) | Tenant scopes map to SE groups and VNets |
| Kubernetes (AKO) | Cilium (LB IPAM + BGP, Gateway API) or HAProxy Ingress | Only if K8s clusters run on the new platform |
| UDP virtual services | Keepalived LVS (IPVS) | WireGuard, DNS, syslog; HAProxy Community has no generic UDP proxy |

## Target architecture

Each tenant gets one or more SE groups of HAProxy VMs on networks defined in the UI, and each SE group places its VIPs with BGP or VRRP, chosen per group. Phase 1 is one tenant with one 2-VM SE group. Config comes only from the controller; if it is down, traffic keeps flowing.

> Diagram (target architecture · controller, SE group, supporting services): see the [online doc](https://claude.ai/code/artifact/bf06390f-6cf2-4fa9-9128-b6e8286ecb93).

- **Data plane:** each SE group is 2+ LB VMs from one template. **BGP mode:** FRR advertises each VIP /32 only while HAProxy is healthy (route health injection), ECMP across the VMs, BFD to the upstream peers. **VRRP mode:** Keepalived holds the VIPs on a shared network, with a check script that drops priority when HAProxy fails. Avi placed VIPs here with static routes on an NSX Tier-1, so both modes are new and neither depends on today's routing.
- **Tenancy:** tenants own their networks, VIP pools, SE groups, certificates and services, and RBAC scopes users to tenants. An SE group can serve several tenant networks with multi-NIC VMs, or a tenant can get dedicated SE groups for isolation.
- **Networking is net-new:** the UI defines networks (Proxmox SDN VNet + subnet, created via the Proxmox API or attached if they exist), VIP IP pools, and per SE group the upstream BGP peers, ASN and BFD, or the VRRP settings and static routes. Nothing is imported from current routing.
- **Control plane:** the controller talks only to management interfaces; nothing in the request path depends on it.
- **Sizing start point:** 2 vCPU / 4 GB per SE VM for light groups, 4–8 vCPU with HAProxy `nbthread` matched to cores for TLS-heavy ones; scale out by adding VMs to a BGP-mode group.

## Controller and dashboard (custom build)

Build a desired-state controller with Avi's object model, not a config-file editor: users declare virtual services, pools and policies; a reconciler renders, validates and pushes them to every engine, and reports drift.

### Scope (MVP first)

1. **Phase 1 (net-new, multi-tenant model, sized for today's services):**
   - Tenants and RBAC with OIDC; every object below is owned by a tenant.
   - Networks: create Proxmox SDN VNets and subnets through the Proxmox API, or attach existing ones. VIP IP pools with built-in IPAM.
   - SE groups: deploy SE VMs from a template through the Proxmox API; choose HA mode (BGP: ASN, peers, BFD; VRRP: VRID, priorities); static routes per group.
   - Virtual services: L7 HTTP/HTTPS with SNI host routing, L4 TCP, and L4 UDP via IPVS. Pools, health monitors, persistence, and HTTP redirect/rewrite rules.
   - Certificates: import, Cloudflare Origin CA, and ACME DNS-01 via Cloudflare, wildcard or per-host, with expiry alerts.
   - Cloudflare-only source allow list and real client IP per virtual service.
   - Render → `haproxy -c` validate → push → Git commit, one-click rollback, live status, enable/disable/drain servers.
2. **Phase 2:** WAF policies with Coraza, rate limits and IP groups, Cloudflare DNS records created from a virtual service, Kubernetes connector per tenant, pool discovery by Proxmox tags, an Avi config importer, health scores, SE scale-out.
3. **Phase 3:** GSLB on PowerDNS across sites, per-tenant quotas, tenant self-service portal, packet capture jobs.

### Stack

- **Backend:** Python 3.12 + FastAPI + SQLAlchemy, OpenAPI schema generated from Pydantic models. Go is the alternative if a single static binary matters more than speed of development.
- **Database:** PostgreSQL (Patroni pair for HA). Every committed change is also written as YAML to a GitLab repo for audit and rollback.
- **Workers:** a reconcile loop per SE group (asyncio tasks or a Redis-backed queue). Changes go out as one HAProxy Data Plane API transaction per group, so a failed push leaves the old config running.
- **Southbound adapters:** HAProxy Data Plane API (config + runtime), FRR via a small node agent (`vtysh` wrapper over mTLS), PowerDNS HTTP API, Proxmox VE API, Prometheus and Loki query APIs, ACME client.
- **Frontend:** React + TypeScript, Grafana panels embedded for traffic graphs instead of rebuilding charting.
- **CLI:** a thin client over the same API (`syctl vs list`, `syctl pool drain web01`), usable from PowerShell 7 or bash.
- **Auth:** OIDC (Authentik, Keycloak or Entra ID); roles = admin, tenant-admin, operator, read-only, each scoped to tenants.

### Core objects

| Object | Key fields | Renders to |
| --- | --- | --- |
| Tenant | name, OIDC group, quotas | RBAC scope over everything below |
| Network | Proxmox SDN zone/VNet, subnet, gateway, VLAN/VNI | Proxmox SDN objects + SE VM NICs |
| VIP pool | network, IP ranges, tenant | Built-in IPAM allocations |
| SE group | tenant, networks, VM template, size, VM count, HA mode | SE VMs via the Proxmox API |
| Routing profile | BGP ASN, peers, BFD, prefix limits; or VRRP VRID and priorities; static routes | FRR or Keepalived config per VM |
| Virtual service | VIP (from a pool), ports, protocol (HTTP, TCP, UDP), TLS profile, cert refs, policies, WAF policy, source allow list | HAProxy `frontend` or IPVS service + VIP advertisement |
| Pool | members (static or tag selector), algorithm, persistence, health monitors | HAProxy `backend` or IPVS real servers |
| Health monitor | type, interval, rise/fall, HTTP request/expect | `option httpchk` / `http-check` lines |
| Certificate | source (upload, ACME via Cloudflare DNS-01, Cloudflare Origin CA, step-ca), scope (wildcard or per-host), SANs, expiry | `crt-list` entry, hot-loaded |
| WAF policy | mode, CRS paranoia level, exclusions | Coraza SPOA directives |
| GSLB service | FQDN, site members, algorithm, health URL | PowerDNS LUA record |

### Dashboard views

- Tenant switcher and fleet overview: every virtual service with health, RPS, p95 latency, 5xx rate, cert expiry.
- Infrastructure builder: networks, VIP pools and IP usage, SE groups (deploy, resize, add VMs), routing profiles with live BGP session state or VRRP master per VIP.
- Virtual service editor: guided create (VIP from a pool, ports, certificate, pool members, health check, policies) with a config diff preview before push.
- VS detail: pool member status (up/down/drain), live request log tail from Loki, latency breakdown, config history.
- Change log: who changed what, Git diff, one-click rollback to any prior commit.

## Migration phases

Build the platform net-new, recreate each service through the UI on new networks and VIPs, test it, then move its public DNS or Cloudflare origin to the new VIP; Avi stays live until the last service moves.

1. **Inventory (done).** The Migration inventory table lists what to recreate; nothing needs a DataScript rewrite or a GSLB move.
2. **Controller phase 1.** Build the phase 1 scope above, since the UI creates the networks, SE groups and routing. Test it in a lab tenant before anything production.
3. **SE template and observability.** Debian SE image (HAProxy + Data Plane API, Keepalived, FRR, IPVS, Coraza SPOA, Vector, node exporter) built by cloud-init + OpenTofu. Stand up Prometheus, Loki, Grafana, and OpenBao for secrets.
4. **First tenant from the UI.** Create the tenant, its networks and VIP pool, one 2-VM SE group with its HA mode and upstream peers, and import the 7 Cloudflare Origin certs.
5. **Pilot.** Recreate mealie.home.example and IPAM, test them on the new VIP, then move each hostname's Cloudflare origin or DNS record to it. Rollback = point the record back.
6. **SNI services.** Recreate the other SNI services with an HTTP health check each (Avi had none), the Nextcloud redirects, WHMCS persistence, and the UniFi ports.
7. **L4, UDP and Kubernetes.** Recreate the K8S PRD CLUS 01 ports (or move them to kube-vip) and WireGuard as an L4 UDP service. Move k8s-prod-a off AKO to in-cluster LoadBalancer tools, renew the expired ranch.k8s-a cert, then uninstall AKO.
8. **Decommission.** Remove Avi SEs and controllers after 30 days with no rollbacks; add further tenants and SE groups from the UI as needed.

## Testing, documentation and risks

### Testing

- **Controller unit tests (pytest):** golden-file tests for every renderer (object in → exact HAProxy/FRR/PowerDNS config out), schema validation, RBAC rules.
- **Integration tests:** Docker Compose or containerlab with real HAProxy + Data Plane API, FRR and PowerDNS; assert pushes apply, `haproxy -c` gate rejects bad config, failed transactions roll back.
- **Failover tests:** kill HAProxy, kill the VM, drop a BGP session; measure VIP convergence (target under 3 s with BFD on the upstream peering); repeat for VRRP groups.
- **Parity tests per migrated VS:** replay a captured request set against Avi and the new VIP; compare status codes, headers, persistence and TLS (testssl.sh).
- **Load tests:** k6 or wrk2 at expected peak ×2 per SE pair before bulk migration.
- CI runs all of the above in GitLab on every merge request.

### Documentation

- Repo `docs/`: architecture, object model, API reference (generated OpenAPI), runbooks for VIP failover, cert rotation, adding a tenant, rollback.
- Inline docstrings on every renderer and adapter; README with local dev setup.

### Risks and gaps vs Avi

| Risk / gap | Mitigation |
| --- | --- |
| No ML anomaly detection or auto-learned WAF | Grafana alert rules on error rate and latency baselines; WAF tuning by log review |
| SE autoscale is not automatic at first | Size SE groups for peak; add controller-driven scale-out in v3 |
| Many VS on one HAProxy means reloads touch all of them | Runtime API for server changes (no reload); split large tenants into their own SE groups |
| DataScripts need manual Lua rewrites | Inventory early; budget time per script |
| No vendor support | Pin versions, keep config in Git, document runbooks; HAProxy Enterprise is a paid fallback for the data plane only |
| Controller becomes a single point of failure | Data plane runs independently of the controller; controller outage only blocks changes |
| FRR on LB VMs vs Proxmox SDN's FRR | LB FRR lives inside guest VMs only; peers only with upstream routers defined in the UI, never with PVE hosts |

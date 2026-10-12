# Competitive parity matrix and phased plan

Date: 2026-10-12. Planning only: no code changes. Compares Switchyard on `origin/main` (M1 to M6 plus the work in
`06-planned-vs-built.md`) with FortiADC, F5 BIG-IP (LTM, GTM/DNS, ASM/AWAF, APM), Citrix NetScaler ADC and Gateway,
AWS Elastic Load Balancing (ALB, NLB, GWLB) and Azure (Traffic Manager, Load Balancer, Application Gateway, Front Door).
Revision 2 adds NetScaler and an identity, zero-trust and security section (2.8).

Sources: vendor documentation and datasheets, [FortiADC handbook](https://docs.fortinet.com/document/fortiadc/7.4.3/handbook/869500/server-load-balance),
[F5 LTM profiles reference](https://techdocs.f5.com/kb/en-us/products/big-ip_ltm/manuals/product/big-ip-local-traffic-management-profiles-reference-14-0-0/09.html),
[F5 GTM wide IP reference](https://clouddocs.f5.com/cli/tmsh-reference/v16/modules/gtm/gtm_wideip_a.html),
[Azure Traffic Manager routing methods](https://learn.microsoft.com/en-us/azure/traffic-manager/traffic-manager-routing-methods),
[NetScaler nFactor](https://docs.netscaler.com/en-us/citrix-adc/current-release/aaa-tm/authentication-methods/multi-factor-nfactor-authentication.html),
[NetScaler rewrite](https://docs.netscaler.com/en-us/citrix-adc/current-release/appexpert/rewrite) and
[responder](https://docs.netscaler.com/en-us/citrix-adc/current-release/appexpert/responder),
AWS Elastic Load Balancing user guide. Where a row rests on general product knowledge and not on a page read for this
document, treat it as a lead to confirm before an ADR is written. Vendor feature names change between releases.

## 1. What Switchyard is, for fit

Switchyard replaces Avi. It is a controller plus HAProxy SEs, with VIPs placed by BGP (FRR) or VRRP (Keepalived), UDP by
IPVS, on Proxmox, multi-tenant with RLS and RBAC. It is **not** a hardware ADC, a cloud-managed LB or a CDN. That sets
what fits:

- **Fits:** anything HAProxy 3.2 does in its config (L4/L7 balancing, ACL-driven policy, stick tables, rate limits,
  compression, cache, Lua, SPOE for WAF), Keepalived/IPVS (L4 modes, UDP), FRR (BGP, BFD, ECMP), DNS via PowerDNS (phase 3),
  the controller's own pipeline (RBAC, audit, change sets, canary).
- **Does not fit:** FPGA/ASIC offload and SSL hardware (HSM aside), a global anycast edge (Front Door, CDN), proprietary
  signature feeds, Layer-2 transparent bridge mode, GWLB-style Geneve service insertion (would need an appliance chain),
  and anything that makes the data plane depend on the controller (CLAUDE.md "Things not to do").

## 2. Parity matrix

NetScaler has no column of its own in 2.1 to 2.7: its capabilities match the FortiADC and F5 columns there (L4/L7
balancing, persistence, rewrite and responder policies, content switching, GSLB, WAF). The differences that matter are in
2.8 and 2.9.

Legend. Switchyard column: **done** (on main), **part** (built with a limit), **plan** (in 01 or 06 as deferred), **no**
(not planned). Fit: **HAProxy**, **IPVS**, **FRR**, **ctrl** (controller work), **DNS** (PowerDNS), **ext** (needs a new
component), **n/a** (does not fit). Priority: **M** must, **S** should, **L** later, **-** skip.

### 2.1 Load balancing core

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| L4 TCP virtual service | yes | yes | NLB | LB | done | HAProxy | |
| L4 UDP virtual service | yes | yes | NLB | LB | done (IPVS, ADR 0013, 0020) | IPVS | |
| L7 HTTP/S, host and path routing | yes | yes | ALB | App GW | done | HAProxy | |
| HTTP/2 to clients | yes | yes | ALB | App GW | done (`http2`) | HAProxy | |
| HTTP/3 (QUIC) to clients | yes | yes | ALB (2024+), CloudFront | Front Door | no (01 lists it, no field) | HAProxy | S |
| gRPC routing and health | yes | yes | ALB | App GW | no (HTTP/2 only, no gRPC check) | HAProxy | S |
| WebSocket | yes | yes | ALB | App GW | works as HTTP, untested and undocumented | HAProxy | S |
| Algorithms: RR, least conn, source hash, random | yes | yes | RR, LOR, random (ALB), flow hash (NLB) | hash, 5-tuple | done (4) | HAProxy | |
| Weighted RR, ratio, dynamic ratio, fastest, observed, predictive | yes | yes | no | no | weights only | HAProxy | L |
| URI, header and parameter hash balancing | yes | iRule | no | no | no (HAProxy has `balance uri/hdr/url_param`) | HAProxy | S |
| Server weights, drain, disable | yes | yes | yes (deregistration delay) | yes | done | HAProxy | |
| Slow start | yes | yes | ALB | no | done (ADR 0066) | HAProxy | |
| Connection limits per member / per service | yes | yes | no | no | `maxconn` on group only | HAProxy | S |
| Backup / standby members | yes | yes | no | no | no (`backup` keyword) | HAProxy | S |
| Pool groups, priority groups (failover pool) | yes | priority groups | no | no | no | HAProxy | S |
| Backend TLS, verify | yes | yes | ALB | yes | done (`tls`, `tls_verify`) | HAProxy | |
| Backend mTLS (client cert to servers) | yes | yes | no | App GW | no (06: missing) | HAProxy | S |
| Connection reuse / multiplexing (OneConnect) | yes | OneConnect | ALB | App GW | HAProxy default; no knob | HAProxy | L |
| Source NAT / SNAT pools | yes | SNAT pools | NLB IP preserve | LB | not exposed | HAProxy+nft | S |
| Direct server return, L2/L3 modes | yes | nPath | GWLB (Geneve) | no | IPVS DR and tunnel (ADR 0192) | IPVS | L |
| Transparent / bridge mode | yes | yes | no | no | no | n/a | - |
| PROXY protocol | yes | yes | NLB | no | done (accept side) | HAProxy | |
| Send PROXY protocol to backends | yes | yes | NLB | no | no | HAProxy | S |
| Static IP / Elastic IP per service | n/a | yes | NLB | LB | done (VIP pools) | ctrl | |
| IPv6 and dual stack | yes | yes | yes | yes | done (ADR 0078) | all | |
| TLS passthrough with SNI routing (L4) | yes | yes | NLB TLS | no | no (TCP service has one pool) | HAProxy | S |

### 2.2 Health checks, persistence, availability

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| TCP and HTTP checks with expect | yes | yes | yes | yes | done | HAProxy | |
| UDP check | yes | yes | NLB | no | done | IPVS | |
| HTTPS, body match, header, expect regex | yes | yes | ALB (code ranges) | probes | status only | HAProxy | S |
| DNS, SMTP, LDAP, MySQL, Redis etc. checks | yes | yes | no | no | no (`tcp-check` can send/expect) | HAProxy | S |
| Custom / script checks | yes | external monitors | no | no | no, by design (ADR 0066) | n/a | - |
| Passive checks (outlier ejection) | yes | yes | no | no | done | HAProxy | |
| Composite checks, minimum healthy members | yes | min up members | no | no | no | HAProxy | L |
| Persistence: source IP | yes | yes | NLB | LB | done | HAProxy | |
| Persistence: cookie insert / rewrite | yes | yes | ALB (stickiness) | App GW affinity | no. Avi parity gap | HAProxy | **M** |
| Persistence: header, URI, SSL session, RDP | yes | yes | no | no | no | HAProxy | S |
| Persistence table sync between SEs | yes | mirroring | n/a | n/a | no (HAProxy `peers`) | HAProxy | S |
| Connection draining on delete | yes | yes | yes | yes | `drain` state; no timed drain | HAProxy | S |
| Active/active VIP, ECMP | yes | yes (ECMP via route domain) | managed | managed | done (BGP) | FRR | |
| Active/standby VIP | yes | yes | managed | managed | done (VRRP) | Keepalived | |
| Config sync between peers | yes | ConfigSync | n/a | n/a | n/a: controller pushes to all | ctrl | |
| Session mirroring / state failover | yes | yes | no | no | no, stateful failover not supported | n/a | - |
| Cross-zone / site failover | yes (GSLB) | GTM | yes | yes | one site per controller | DNS | L |

### 2.3 TLS and certificates

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SNI, multiple certs per port | yes | yes | yes | yes | done (crt-list) | HAProxy | |
| TLS versions and cipher profiles | yes | yes | security policies | yes | done (ADR 0067) | HAProxy | |
| OCSP stapling | yes | yes | n/a | n/a | done | HAProxy | |
| Client certificate auth, CRL | yes | yes | ALB mTLS | App GW | done (ADR 0070); OCSP for clients no | HAProxy | |
| ACME / managed certificates | yes (Let's Encrypt) | no | ACM | managed certs | done (DNS-01, step-ca, Cloudflare) | ctrl | |
| ACME HTTP-01 / TLS-ALPN-01 | yes | no | n/a | n/a | in flight (Backend gaps thread) | ctrl | S |
| HSM / external key store | yes | yes | KMS | Key Vault | keys in OpenBao | ctrl | L |
| SSL forward proxy / inspection | yes | yes | no | no | no | ext | - |
| Certificate expiry alerts | yes | yes | ACM | yes | renew job; no alert channel | ctrl | S |
| Session tickets, session cache control | yes | yes | no | no | HAProxy defaults | HAProxy | L |
| Early data, TLS 1.3 only | yes | yes | yes | yes | `modern` profile | HAProxy | |

### 2.4 HTTP policy, content manipulation, performance

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Redirects (HTTP to HTTPS, rules) | yes | yes | ALB | App GW | done | HAProxy | |
| Fixed response, custom error pages | yes | yes | ALB | App GW | no | HAProxy | **M** |
| Header insert, remove, rewrite (request and response) | yes | yes | ALB (limited) | App GW rewrite | no. Avi parity gap, Nextcloud-style rules need it | HAProxy | **M** |
| URL rewrite | yes | yes | ALB (rewrite) | App GW | no | HAProxy | **M** |
| Match on header, method, query, source IP, SNI | yes | yes | ALB | App GW | host and path only | HAProxy | **M** |
| Weighted routing between pools (canary, blue-green) | yes | yes | ALB | Front Door | no | HAProxy | S |
| Rate limiting per source / per URL | yes | yes | WAF rate rules | Front Door WAF | no (01: phase 2) | HAProxy | **M** |
| Connection and request limits per client | yes | yes | no | no | no | HAProxy | **M** |
| Geo / IP reputation allow and deny | yes (FortiGuard) | yes (IP intelligence) | WAF geo | Front Door geo filter | allow list (CIDR) only | HAProxy | S |
| Compression | yes | yes | no | Front Door | no (HAProxy `compression`) | HAProxy | S |
| Caching | yes | yes (WebAcceleration) | CloudFront | Front Door | no | HAProxy | L |
| HTTP/1.1 to HTTP/2 to backend | yes | yes | ALB (gRPC) | App GW | no | HAProxy | L |
| Scripting (iRules, FortiADC scripts, DataScript) | Lua-like | iRules (Tcl) | no | no | no, by design (no free-form snippets) | n/a | - |
| Controlled Lua | - | - | - | - | n/a: allow only named, reviewed Lua actions | HAProxy | L |
| Authenticated reverse proxy / SSO (OIDC, SAML) | yes | APM | ALB OIDC and Cognito | App GW (no), Front Door (no) | no. 01: oauth2-proxy | HAProxy+ext | S |
| Bot defense | yes | AWAF / Bot Defense | WAF Bot Control | Front Door WAF | no | ext | L |
| DDoS, SYN protection | yes | yes (AFM) | Shield | DDoS Protection | no. Kernel SYN cookies, nftables | ext | L |

### 2.5 Security: WAF and access

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Signature WAF (OWASP CRS) | yes | ASM/AWAF | AWS WAF (managed rules) | App GW WAF, Front Door WAF | no. 01: Coraza SPOA, phase 2 | ext | S |
| Detection-only vs blocking per policy | yes | yes | count mode | detection mode | plan | ext | S |
| Learning / auto-tune | yes | yes | no | no | no, tune by log review (01) | n/a | - |
| API protection (OpenAPI schema validation) | yes | yes | WAF body rules | no | no | ext | L |
| DLP, credit-card masking | yes | yes | no | no | no | n/a | - |
| Virtual patching with scanner import | yes | yes | no | no | no | n/a | - |
| WAF policy per tenant, versioned | yes | yes | web ACLs | policies | plan (`waf_policy` table) | ctrl | S |
| Allowed-source list (Cloudflare ranges) | partial | partial | security groups | NSG | done (managed list) | HAProxy | |
| Network ACL / L3-L4 firewall policy | yes | AFM | NACL, SG | NSG | no | nftables | L |

### 2.6 Global and DNS load balancing

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Authoritative DNS with health-checked records | GSLB | GTM / DNS | Route 53 | Traffic Manager | no. 01: PowerDNS, phase 3 | DNS | L |
| Priority (failover) routing | yes | global availability | failover | Priority | no | DNS | L |
| Weighted / ratio | yes | ratio | weighted | Weighted | no | DNS | L |
| Geo / topology | yes | topology | geolocation | Geographic | no | DNS | L |
| Performance / latency based | yes | RTT, QoS | latency | Performance | no | DNS | L |
| Multivalue answer | yes | yes | multivalue | Multivalue | no | DNS | L |
| Subnet (client IP range) routing | yes | topology | IP-based | Subnet | no | DNS | L |
| DNSSEC | yes | yes | yes | no | no (PowerDNS supports it) | DNS | L |
| LDNS persistence | yes | yes | no | no | no | DNS | L |
| DNS firewall / rate limit (dnsdist) | yes | DNS Express, DoS | Resolver FW | no | no | DNS | L |
| Link load balancing (multi-WAN) | yes | yes (LTM routing) | n/a | n/a | n/a: SEs are not edge routers | n/a | - |

### 2.7 Operations, observability, platform

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| REST API, declarative config | yes | AS3 / iControl | API | ARM | done (OpenAPI, change sets) | ctrl | |
| Config diff, rollback, audit, approvals | basic | basic | CloudTrail | Activity Log | done, ahead of most | ctrl | |
| Multi-tenancy with RBAC, quotas | VDOM / admin domains | partitions, route domains | accounts | subscriptions | done (RLS, SAML/OIDC) | ctrl | |
| Per-service metrics and logs | yes | yes | CloudWatch | Monitor | partial: API done, pipeline not (06) | ctrl+ext | **M** |
| Tenant-scoped dashboards (Grafana) | built in | Analytics | CloudWatch | Workbooks | in flight (Embedded metrics thread) | ext | **M** |
| Alerts and notifications | yes | yes | CloudWatch alarms | Monitor | events/SSE; no outbound webhook or mail | ctrl | **M** |
| Health score | yes | no | no | no | plan | ctrl | S |
| Request logs with per-request fields | yes | yes | access logs | diagnostic logs | HAProxy log lines; Vector/Loki plan | ext | **M** |
| Packet capture | yes | tcpdump | traffic mirroring | packet capture | agent `tcpdump` job (ADR 0194) | ctrl | L |
| Auto scaling of ADC instances | yes (cloud) | yes (cloud) | automatic | automatic | manual deploy and retire | ctrl | S |
| Upgrade orchestration (rolling SE upgrade) | yes | yes | n/a | n/a | via image rebuild; no job | ctrl | S |
| Backup / restore of controller | yes | UCS | n/a | n/a | in the ops plan (pgBackRest) | ctrl | **M** |
| Terraform / Ansible provider | yes | yes | yes | yes | OpenAPI only; OpenTofu provider later | ctrl | S |
| Kubernetes ingress / Gateway controller | yes | CIS | AWS LB Controller | AGIC | plan (phase 2) | ctrl | L |
| Service discovery (VM tags, cloud APIs) | yes | yes | target groups | backend pools | plan (Proxmox tags) | ctrl | S |
| Importers from another vendor | no | no | no | no | Avi importer planned | ctrl | L |
| Service insertion (GWLB / appliance chaining) | no | no | GWLB | Gateway LB | no | n/a | - |
| Global edge / CDN / anycast | no | no | CloudFront, Global Accelerator | Front Door | no | n/a | - |

### 2.8 Identity, zero trust and advanced security

Authentication in front of a service is where F5 APM, NetScaler AAA/Gateway (nFactor) and FortiADC auth profiles do their
most valuable work. AWS (ALB OIDC/Cognito) and Azure (Entra, Front Door WAF) cover less of it at the load balancer.
Switchyard has none of it on the data path: login exists only for the controller (ADR 0012, 0018). Zero trust here means:
every request carries an identity, policy decides per request (who, which app, which path, which device), and the origin
is not reachable around the proxy.

| Capability | FortiADC | F5 | NetScaler | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| OIDC login in front of an HTTP service | yes | APM | AAA / nFactor | ALB OIDC | Entra, Front Door | no | HAProxy + auth service | **M** |
| SAML SP login | yes | APM | AAA | via Cognito | Entra | no | auth service | S |
| LDAP / RADIUS / local users | yes | APM | AAA | no | no | no | auth service | S |
| Multi-step conditional auth flows | basic | APM policy editor | nFactor | no | Conditional Access | no | auth service | L |
| MFA (TOTP, push, WebAuthn) | yes | APM | nFactor | Cognito | Entra | delegated to the IdP | IdP | S |
| SSO to backends (identity headers, JWT, Kerberos delegation) | yes | APM | AAA / Gateway | ALB claims header | App Proxy | no | auth service | **M** (headers), L (Kerberos) |
| Per-route authorization (group, claim, path) | yes | APM | AAA | ALB rules | no | no | ctrl + auth service | **M** |
| Sessions: cookie, idle and absolute timeout, logout, revoke | yes | APM | AAA | ALB | no | no | auth service | **M** |
| Client certificate auth (mTLS) | yes | yes | yes | ALB mTLS | App GW | done (ADR 0070) | HAProxy | |
| Certificate to identity mapping, OCSP for clients | yes | APM | AAA | no | no | CRL only | HAProxy + ctrl | S |
| JWT validation for APIs | yes | yes | yes | API Gateway | APIM | no | HAProxy `jwt_verify` | S |
| Device posture / endpoint checks | no | APM | Gateway EPA | Verified Access | Conditional Access | no | ext | L |
| ZTNA or VPN gateway for non-HTTP apps | SSL VPN | APM | Gateway | Client VPN, Verified Access | Entra Private Access | no | ext | L |
| Origin lock-down (only the proxy reaches the app) | yes | yes | yes | security groups | NSG | done for Cloudflare ranges | HAProxy + nft | |
| Rate limits on login paths, credential-stuffing controls | yes | yes | yes | WAF | WAF | no | HAProxy | S (with A5) |
| Bot management | yes | Bot Defense | Bot Mgmt | WAF Bot Control | Front Door bot rules | no | ext (CrowdSec, challenge) | L |
| DDoS (SYN, L4 floods, L7 floods) | yes | AFM / DoS | yes | Shield | DDoS Protection | no | nft + HAProxy | L |
| API security (schema validation, per-key quotas) | yes | AWAF API | API Security | API Gateway | APIM | no | ext | L |
| Audit of auth events | yes | yes | yes | CloudTrail | Monitor | controller audit only | ctrl | **M** |

### 2.9 NetScaler-specific items

| Capability | Note | Fit | Pri |
| --- | --- | --- | --- |
| Rewrite and responder policies (expressions, bind points, priorities) | Same job as A2 to A4. Switchyard offers a structured match and action model, not an expression language (no free-form snippets). | HAProxy | M (A2 to A4) |
| Content switching virtual servers | Equals host and path routes to pools. | done | |
| AppQoE, priority queueing | HAProxy queues, `maxconn`, `timeout queue`. | HAProxy | L |
| HTTP callouts (external decision per request) | HAProxy SPOE, same sidecar pattern as WAF and auth. | ext | L |
| Gateway portal theming | Branding of the auth service login page. | ext | L |
| Clustering, SDX/MPX hardware tenancy | Replaced by SE groups and tenants. | n/a | - |
| Integrated caching, front-end optimization | See C5. | HAProxy | L |

## 3. What the matrix says

1. **Core L4/L7 is at parity or ahead** on tenancy, audit, change control and IPv6. The gaps are not in routing or TLS.
2. **The real distance is HTTP policy.** Cookie persistence, header and URL rewrite, richer match conditions, fixed
   responses and rate limits are in every competitor and in Avi, and are missing here. They are all HAProxy features, so
   the work is model, validator, renderer and UI, not new infrastructure.
3. **Observability is the biggest operational gap** and is already in flight (metrics proxy, Vector/Loki). Alerting
   (outbound notifications) is the part nobody owns yet.
4. **Data-path authentication is the second big gap, and the one that makes zero trust possible.** F5 APM, NetScaler
   AAA and FortiADC auth profiles put login, MFA, per-route authorization and session handling in front of any
   application. Switchyard has the controller's own OIDC/SAML login but nothing on the data path. mTLS, the origin
   lock-down and the audit trail already exist; the missing piece is an identity-aware proxy (an auth service next to
   HAProxy). See phase E.
5. **WAF is a product decision.** F5 and Fortinet sell it as the headline. For Switchyard it is a Coraza SPOA sidecar with
   CRS; the cost is the sidecar life cycle and false-positive tuning, not rendering.
6. **GSLB is a separate subsystem** (PowerDNS, dnsdist). It is where Azure Traffic Manager and F5 GTM live. It does not
   block anything else and has no tenant data-plane dependency, so it can wait.
7. **Not worth chasing:** scripting engines (iRules and friends), SSL inspection, link load balancing, bridge mode,
   service insertion and CDN. They fight the "no free-form snippets" and "data plane independent of the controller" rules
   or need a different product.

## 4. Phased plan

Size: S = days (one MR), M = 1 to 2 weeks, L = several weeks. Each item that adds config surface needs an ADR, a golden
test, a field-rule entry and a UI form (the UI trails the backend by one step).

### Phase A: HTTP policy parity (must)

| # | Item | Size | Depends on | Notes |
| --- | --- | --- | --- | --- |
| A1 | Cookie persistence (insert, rewrite, prefix) with TTL; persistence on header and URI | M | none | New `persistence` kinds beside `source_ip`; HAProxy `cookie`, `stick on`. Avi parity. |
| A2 | Request/response header actions (set, add, remove, with a safe variable list) and URL rewrite | M | none | New policy set `kind`, strict header and path validators, ordered into the request-order list in `docs/render.md`. |
| A3 | Richer route matches: method, header, query, source CIDR, SNI | M | A2 (shares the condition model) | Extend `RouteMatch`; keep one condition type for routes and policies. |
| A4 | Fixed responses and custom error pages | S | A2 | `http-request return`, `errorfile`. Error bodies are text uploaded as a policy, not free-form config. |
| A5 | Rate limits and connection limits (per source, per path), tarpit/deny | M | A3 | New policy set `kind`; stick-table counters; per-service and per-tenant ceilings in quota. Phase 2 in 01. |
| A6 | Member options: `backup`, per-member `maxconn`, timed drain; `send-proxy` to backends | S | none | Pool and member fields. |
| A7 | Backend mTLS (client cert on server lines) | S | ADR 0070 | Open item in 06. |
| A8 | Health checks: HTTPS body/header match, `tcp-check` send/expect presets (Redis, SMTP, LDAP, MySQL) | M | none | Named presets only; no custom command (ADR 0066 stands). |

### Phase B: Operate it (must)

| # | Item | Size | Depends on | Notes |
| --- | --- | --- | --- | --- |
| B1 | Telemetry pipeline live (exporter, Prometheus, Vector, Loki) and tenant-scoped Grafana proxy | L | running threads | Already in flight. |
| B2 | Alerts: outbound webhook, email and chat targets per tenant; rules on events and metrics | M | B1 | Alertmanager or controller-side; one tenant-scoped contact model. |
| B3 | Request-log view in the UI (Loki query behind the API) | M | B1 | API from A9 exists. |
| B4 | Controller image, production Compose, Patroni, pgBackRest, SE image recipe, runbooks | L | Operations thread | From 06 section 2. |
| B5 | Live checks: `.p12`, conflict view, end-to-end lab in CI | M | B4 | From 06. |

### Phase C: Differentiators that stay on-architecture (should)

| # | Item | Size | Depends on | Notes |
| --- | --- | --- | --- | --- |
| C1 | HTTP/3 on ports, gRPC and WebSocket as explicit options, with checks and tests | M | none | QUIC needs UDP 443 bind and a tested HAProxy 3.2 config. |
| C2 | Weighted pool routing (canary, blue-green) and pool priority groups | M | A3 | Route target becomes a weighted list of pools. |
| C3 | Persistence table sync (`peers`) and SNAT options | M | A1 | Peer addresses on the SE group's backend network. |
| C4 | TLS passthrough with SNI routing on TCP services | S | none | `req.ssl_sni` on a TCP frontend; no certificate. |
| C5 | Compression; then cache | S, M | A2 | Per service flags. |
| C6 | Coraza WAF: `waf_policy`, SPOA sidecar in the SE image, detect-first, CRS version pinned | L | B1 (logs), B4 (image) | ADR needed for sidecar life cycle and failure mode (fail-open vs fail-closed). |
| C7 | ACME HTTP-01 and TLS-ALPN-01; certificate expiry notifications | M | B2 | HTTP-01 needs a route to the controller or a challenge responder on the SE. |
| C8 | Moved to phase E (E1). | | | |
| C9 | Pool discovery by Proxmox tags; health score; rolling SE upgrade job; scale-out | L | B4 | Already in 01/06. |
| C10 | OpenTofu provider generated from the OpenAPI | M | none | Wraps the API; no server work. |
| C11 | Geo and IP-reputation lists (managed lists like the Cloudflare one, MaxMind, CrowdSec bouncer) | M | A5 | Reuse the managed allow-list job pattern. |

### Phase E: Identity-aware access and zero trust (E1 to E4 must, rest should or later)

Architecture: an **auth service** per SE group (a sidecar in the SE image, for example oauth2-proxy or a small Go
service). HAProxy calls it for each request on a protected route (SPOE or an auth subrequest; 01 already names this).
Sessions are signed cookies, so the SE needs no shared store. The controller owns the config (tenant IdP, route policy);
the SE keeps serving existing sessions if the controller is down; secrets are `secret_ref`s into OpenBao.

| # | Item | Size | Depends on | Notes |
| --- | --- | --- | --- | --- |
| E1 | Auth service in the SE image: OIDC login, session cookie, logout, idle and absolute timeouts. ADR for the call contract and fail mode (closed by default) | L | A3, B4 | Replaces C8. Tenant IdP objects reuse the shape of the controller IdP admin (ADR 0032). |
| E2 | Per-route authorization: require login, or group, claim or email-domain rules, per route or path | M | E1 | New policy set `kind: access`, placed after the connection filter in the request order. |
| E3 | Identity headers to the origin (user, groups, signed JWT); client-supplied copies dropped | S | E1, A2 | Same hygiene rule as the client-IP header in `docs/render.md`. |
| E4 | Auth events in audit and logs (login, deny, logout, revoke), tenant-scoped | S | E1, B1 | Same audit pipeline as the controller. |
| E5 | SAML SP, LDAP/RADIUS and local-user providers | M each | E1 | After OIDC proves the model; most tenants can federate through an IdP. |
| E6 | Client certificate identity: map CN/SAN to a user or group, OCSP for client certs, certificate plus OIDC as two factors | M | E1, ADR 0070 | The NetScaler and F5 "certificate then login" flow. |
| E7 | JWT validation for APIs (issuer, audience, scopes), no login page | M | A3 | HAProxy `jwt_verify`; JWKS fetched and distributed by the controller. |
| E8 | Conditional flows (nFactor-style step-up by path, group or network) | L | E1 | Only if a tenant asks; ordered rules, no visual editor. |
| E9 | Device posture and a ZTNA gateway for non-HTTP apps | L | E1 | Needs a client or an IdP device signal (Entra, Okta). Separate product decision. |
| E10 | Bot defense and DDoS profiles, login-path rate limits, API quotas per key | L | A5, C6 | Was D4. |

### Phase D: Later (separate subsystems)

| # | Item | Size | Depends on |
| --- | --- | --- | --- |
| D1 | GSLB on PowerDNS and dnsdist: priority, weighted, geo, latency, multivalue, subnet, DNSSEC; wide IP = tenant object | L (multi-MR) | B1 (health data), a second site |
| D2 | Kubernetes Service/Gateway connector | L | C9 discovery |
| D3 | Avi importer | M | A1 to A5 (so the objects exist) |
| D4 | Moved to E10. | | |
| D5 | API schema protection in the WAF; packet capture job | M each | C6 / agent |
| D6 | Controlled Lua actions (reviewed, named, per tenant allow) | L | own security review |
| D7 | IPVS DR/tunnel modes for L4 | M | needs L2 adjacency on SE networks |
| D8 | Tenant self-service portal | L | quotas, billing decision |

### Out of scope (does not fit)

Free-form scripting (iRules, NetScaler policy expressions, FortiADC scripts) and arbitrary external checks, SSL inspection proxy, multi-WAN link load
balancing, Layer-2 bridge mode, GWLB-style Geneve service insertion, global anycast edge and CDN, hardware offload.
Revisit only if a tenant requirement makes one of them a decision, with an ADR.

## 5. Risks and open questions

- **Auth service on the request path.** E1 adds a component every protected request touches. Decide the fail mode
  (closed) and use signed-cookie sessions so a sidecar restart does not log everyone out. It must not need the
  controller to serve requests.
- **Identity spoofing.** The origin must trust identity headers only from the SE. Pair E3 with the origin lock-down and
  drop client-supplied copies of those headers.
- **Secrets.** IdP client secrets and session-signing keys are `secret_ref`s into OpenBao, never in PostgreSQL or the
  rendered config.

- **WAF cost.** C6 is the largest single item and carries operational risk (false positives, sidecar crash policy).
  Cloudflare already filters in front for the current tenant; decide whether any tenant needs it before 2027.
- **Policy model size.** A2, A3, A5 and A8 touch the same condition and action model. Do the model design once (one ADR)
  before the first MR, or the renderer order list in `docs/render.md` will sprawl.
- **Header and rewrite safety.** Every value reaching the config needs the strict-validator treatment in CLAUDE.md. No
  `%[...]` log-format expressions from users; offer a fixed variable list.
- **Per-tenant limits.** Rate limits and stick tables use SE memory shared by tenants in a group. Quotas must cap table
  sizes.
- **GSLB without a second site** has no data to protect. Do not start D1 before there is a second SE group at a second
  site, or the health inputs are fiction.
- **Source quality.** Some vendor comparisons above (HTTP/3 on ALB, per-method availability on Front Door) change often.
  Confirm against the current vendor page when an item is picked up.

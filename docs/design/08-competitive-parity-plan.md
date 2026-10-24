# Competitive parity matrix and phased plan

Date: 2026-10-12. Planning only: no code changes. Compares Switchyard on `origin/main` (M1 to M6 plus the work in
`06-planned-vs-built.md`) with FortiADC, F5 BIG-IP (LTM, GTM/DNS, ASM/AWAF, APM), Citrix NetScaler ADC and Gateway,
AWS Elastic Load Balancing (ALB, NLB, GWLB) and Azure (Traffic Manager, Load Balancer, Application Gateway, Front Door).
Revision 2 adds NetScaler and an identity, zero-trust and security section (2.8).

**Revision 3 (2026-10-21) re-verified the "Switchyard now" column against the tree at `fefd4f9`, by reading the code, not
from memory.** A cell that says `done` means a domain field, its validator, the renderer that emits the configuration and
at least one test exist; a cell that says `part` names its limit in the same cell. The local gates passed while doing
this (`ruff`, `mypy --strict`, `pytest -m "not docker"`: 2608 passed) but the 822 container tests did not run, so a row
whose only evidence is one of those is marked on code plus test-exists, not on a run made for this revision. Sections 4
and 5 keep the plan as written; 4.0 records what has since shipped, so the plan stays a record and the matrix stays the
current statement.

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
  compression, cache, SPOE for WAF and auth), Keepalived/IPVS (L4 modes, UDP), FRR (BGP, BFD, ECMP), DNS via PowerDNS
  (GSLB), the controller's own pipeline (RBAC, audit, change sets, canary).
- **Does not fit:** FPGA/ASIC offload and SSL hardware (HSM aside), a global anycast edge (Front Door, CDN), proprietary
  signature feeds, Layer-2 transparent bridge mode, GWLB-style Geneve service insertion (would need an appliance chain),
  and anything that makes the data plane depend on the controller (CLAUDE.md "Things not to do").

## 2. Parity matrix

NetScaler has no column of its own in 2.1 to 2.7: its capabilities match the FortiADC and F5 columns there (L4/L7
balancing, persistence, rewrite and responder policies, content switching, GSLB, WAF). The differences that matter are in
2.8 and 2.9.

Legend. Switchyard column: **done** (on main, with a test), **part** (built with a limit, stated in the cell), **no** (not
built), **n/a** (does not fit). Fit: **HAProxy**, **IPVS**, **FRR**, **ctrl** (controller work), **DNS** (PowerDNS),
**ext** (needs a new component), **n/a** (does not fit). Priority: **M** must, **S** should, **L** later, **-** skip. The
priorities are those of revision 2 and are kept for the record; 4.0 says what is still open.

### 2.1 Load balancing core

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| L4 TCP virtual service | yes | yes | NLB | LB | done | HAProxy | |
| L4 UDP virtual service | yes | yes | NLB | LB | done (IPVS, ADR 0013, 0020) | IPVS | |
| L7 HTTP/S, host and path routing | yes | yes | ALB | App GW | done | HAProxy | |
| HTTP/2 to clients | yes | yes | ALB | App GW | done (`http2`) | HAProxy | |
| HTTP/3 (QUIC) to clients | yes | yes | ALB (2024+), CloudFront | Front Door | done (`http3`: `quic4@`/`quic6@` bind plus `Alt-Svc`; lab pass 2026-10-12) | HAProxy | S |
| gRPC routing and health | yes | yes | ALB | App GW | part: `backend_protocol: h2/h2c` and `tunnel_timeout_s`; no gRPC (or HTTP/2) health check, and streams were not run in the lab | HAProxy | S |
| WebSocket | yes | yes | ALB | App GW | part: works as an HTTP/1.1 upgrade with `tunnel_timeout_s`; nothing to switch on, and `tests/integration/test_websocket_upgrade.py` proves the upgrade and the tunnel timeout | HAProxy | S |
| Algorithms: RR, least conn, source hash, random | yes | yes | RR, LOR, random (ALB), flow hash (NLB) | hash, 5-tuple | done (4) | HAProxy | |
| Weighted RR, ratio, dynamic ratio, fastest, observed, predictive | yes | yes | no | no | weights only | HAProxy | L |
| URI, header and parameter hash balancing | yes | iRule | no | no | done (ADR 0224): `balance uri`, `balance hdr(<name>)` and `balance url_param(<name>)` from `Algorithm` `uri`/`hdr`/`url_param` plus `Pool.hash_key`; `hash-type` stays HAProxy's default | HAProxy | S |
| Server weights, drain, disable | yes | yes | yes (deregistration delay) | yes | done | HAProxy | |
| Slow start | yes | yes | ALB | no | done (ADR 0066) | HAProxy | |
| Connection limits per member / per service | yes | yes | no | no | part: per-member `maxconn`, per-source `conn_cur`/`conn_rate`, per-rule `max_concurrent`; no per-service cap | HAProxy | S |
| Backup / standby members | yes | yes | no | no | done (`backup` on the server line) | HAProxy | S |
| Pool groups, priority groups (failover pool) | yes | priority groups | no | no | done as a route-level chain of up to four pools gated by `nbsrv() gt 0` (needs a health monitor); no standalone pool-group object | HAProxy | S |
| Backend TLS, verify | yes | yes | ALB | yes | done (`tls`, `tls_verify`) | HAProxy | |
| Backend mTLS (client cert to servers) | yes | yes | no | App GW | done (`crt` per server line; the certificate ships in the agent bundle) | HAProxy | S |
| Connection reuse / multiplexing (OneConnect) | yes | OneConnect | ALB | App GW | HAProxy default; no knob | HAProxy | L |
| Source NAT / SNAT pools | yes | SNAT pools | NLB IP preserve | LB | part: automatic masquerade for IPVS NAT (UDP); no configurable SNAT pool | HAProxy+nft | S |
| Direct server return, L2/L3 modes | yes | nPath | GWLB (Geneve) | no | done for IPVS (NAT, DR, tunnel; ADR 0192) | IPVS | L |
| Transparent / bridge mode | yes | yes | no | no | no | n/a | - |
| PROXY protocol | yes | yes | NLB | no | done (accept and send, including `check-send-proxy`) | HAProxy | |
| Send PROXY protocol to backends | yes | yes | NLB | no | done | HAProxy | S |
| Static IP / Elastic IP per service | n/a | yes | NLB | LB | done (VIP pools) | ctrl | |
| IPv6 and dual stack | yes | yes | yes | yes | done (ADR 0078) | all | |
| TLS passthrough with SNI routing (L4) | yes | yes | NLB TLS | no | done (`inspect-delay` plus `req.ssl_sni` maps on a TCP service) | HAProxy | S |

### 2.2 Health checks, persistence, availability

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| TCP and HTTP checks with expect | yes | yes | yes | yes | done | HAProxy | |
| UDP check | yes | yes | NLB | no | done | IPVS | |
| HTTPS, body match, header, expect regex | yes | yes | ALB (code ranges) | probes | part: status, literal body string and literal header match; no `rstatus`/`rstring` regex | HAProxy | S |
| DNS, SMTP, LDAP, MySQL, Redis etc. checks | yes | yes | no | no | done as named `tcp` presets (`redis`, `smtp`, `ldap`, `mysql`); no arbitrary `tcp-check` sequence | HAProxy | S |
| Custom / script checks | yes | external monitors | no | no | no, by design (ADR 0066) | n/a | - |
| Passive checks (outlier ejection) | yes | yes | no | no | done (`observe ... error-limit`) | HAProxy | |
| Composite checks, minimum healthy members | yes | min up members | no | no | no | HAProxy | L |
| Persistence: source IP | yes | yes | NLB | LB | done (stick table, optional `peers` sync) | HAProxy | |
| Persistence: cookie insert / rewrite | yes | yes | ALB (stickiness) | App GW affinity | done (`insert`, `rewrite`, `prefix`; maxlife, secure, httponly) | HAProxy | |
| Persistence: header, URI, SSL session, RDP | yes | yes | no | no | part: header and URI (`stick on req.hdr()` / `path` / `url_param`), HTTP pools only; no SSL-session or RDP persistence | HAProxy | S |
| Persistence table sync between SEs | yes | mirroring | n/a | n/a | done (`peers` block for stick tables) | HAProxy | S |
| Connection draining on delete | yes | yes | yes | yes | done: `drain` state plus a timed `expire_drains` job that flips it to `disabled` | HAProxy | S |
| Active/active VIP, ECMP | yes | yes (ECMP via route domain) | managed | managed | done (BGP) | FRR | |
| Active/standby VIP | yes | yes | managed | managed | done (VRRP) | Keepalived | |
| Config sync between peers | yes | ConfigSync | n/a | n/a | n/a: controller pushes to all | ctrl | |
| Session mirroring / state failover | yes | yes | no | no | no, stateful failover not supported | n/a | - |
| Cross-zone / site failover | yes (GSLB) | GTM | yes | yes | part: GSLB steers DNS, but one site per controller (ADR 0180 is proposed) | DNS | L |

### 2.3 TLS and certificates

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SNI, multiple certs per port | yes | yes | yes | yes | done (crt-list, first entry default) | HAProxy | |
| TLS versions and cipher profiles | yes | yes | security policies | yes | done (ADR 0067: modern, intermediate, legacy) | HAProxy | |
| OCSP stapling | yes | yes | n/a | n/a | done (`[ocsp-update on]`) | HAProxy | |
| Client certificate auth, CRL | yes | yes | ALB mTLS | App GW | done (ADR 0070); OCSP for clients no | HAProxy | |
| ACME / managed certificates | yes (Let's Encrypt) | no | ACM | managed certs | done (DNS-01, HTTP-01, TLS-ALPN-01; step-ca, Let's Encrypt and five other directories) | ctrl | |
| ACME HTTP-01 / TLS-ALPN-01 | yes | no | n/a | n/a | done (`services/http01.py`, `services/tlsalpn.py`) | ctrl | S |
| HSM / external key store | yes | yes | KMS | Key Vault | part: private keys live in OpenBao (KV v2 plus PKI); no HSM or KEK integration | ctrl | L |
| SSL forward proxy / inspection | yes | yes | no | no | no | ext | - |
| Certificate expiry alerts | yes | yes | ACM | yes | done (alert rules over expiry, plus the daily renew job) | ctrl | S |
| Session tickets, session cache control | yes | yes | no | no | HAProxy defaults; no knob | HAProxy | L |
| Early data, TLS 1.3 only | yes | yes | yes | yes | `modern` profile | HAProxy | |

### 2.4 HTTP policy, content manipulation, performance

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Redirects (HTTP to HTTPS, rules) | yes | yes | ALB | App GW | done | HAProxy | |
| Fixed response, custom error pages | yes | yes | ALB | App GW | done (`http-request return`, `http-error`; bodies are validated literals stored as files, never free-form config) | HAProxy | |
| Header insert, remove, rewrite (request and response) | yes | yes | ALB (limited) | App GW rewrite | done (set/add/del header in both phases, fixed variable list) | HAProxy | |
| URL rewrite | yes | yes | ALB (rewrite) | App GW | part: prefix-to-prefix on segment boundaries; no regex rewrite and no query-string manipulation | HAProxy | |
| Match on header, method, query, source IP, SNI | yes | yes | ALB | App GW | done (one ANDed `RouteMatch` shared by routes and every policy rule) | HAProxy | |
| Weighted routing between pools (canary, blue-green) | yes | yes | ALB | Front Door | done (two to eight pools, weights must total 100) | HAProxy | |
| Rate limiting per source / per URL | yes | yes | WAF rate rules | Front Door WAF | part: the stick-table key is the client IP or a request header, so "per URL" is a path match plus that key; at most eight rules per service; counters are per engine | HAProxy | |
| Connection and request limits per client | yes | yes | no | no | done (`conn_cur`/`conn_rate`, per-rule `max_concurrent`) | HAProxy | |
| Geo / IP reputation allow and deny | yes (FortiGuard) | yes (IP intelligence) | WAF geo | Front Door geo filter | done (managed CIDR lists, country lists, CrowdSec decisions) | HAProxy | S |
| Compression | yes | yes | no | Front Door | done (gzip and deflate, media-type list) | HAProxy | S |
| Caching | yes | yes (WebAcceleration) | CloudFront | Front Door | done (HAProxy `cache`, capped per group) | HAProxy | L |
| HTTP/1.1 to HTTP/2 to backend | yes | yes | ALB (gRPC) | App GW | done (`backend_protocol: h2` or `h2c`) | HAProxy | L |
| Scripting (iRules, FortiADC scripts, DataScript) | Lua-like | iRules (Tcl) | no | no | no, by design (no free-form snippets) | n/a | - |
| Controlled Lua | - | - | - | - | part: the JS challenge ships reviewed Lua generated by the controller; users cannot supply Lua | HAProxy | L |
| Authenticated reverse proxy / SSO (OIDC, SAML) | yes | APM | ALB OIDC and Cognito | App GW (no), Front Door (no) | done (oauth2-proxy per access set, Dex broker, login gate) | HAProxy+ext | S |
| Bot defense | yes | AWAF / Bot Defense | WAF Bot Control | Front Door WAF | done (JS proof-of-work challenge, Turnstile/hCaptcha, scanner and user-agent deny, CrowdSec) | ext | L |
| DDoS, SYN protection | yes | yes (AFM) | Shield | DDoS Protection | part: per-source connection and request limits; volumetric floods are deliberately left to the network in front | ext | L |

### 2.5 Security: WAF and access

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Signature WAF (OWASP CRS) | yes | ASM/AWAF | AWS WAF (managed rules) | App GW WAF, Front Door WAF | done (Coraza SPOA, CRS pinned, per-group config) | ext | S |
| Detection-only vs blocking per policy | yes | yes | count mode | detection mode | done (detect or block, fail-open or fail-closed) | ext | S |
| Learning / auto-tune | yes | yes | no | no | no, tune by log review | n/a | - |
| API protection (OpenAPI schema validation) | yes | yes | WAF body rules | no | part: path template, method, query names and patterns, content type, body size and traversal guards as ACLs; no JSON body schema validation | ext | L |
| DLP, credit-card masking | yes | yes | no | no | no | n/a | - |
| Virtual patching with scanner import | yes | yes | no | no | no | n/a | - |
| WAF policy per tenant, versioned | yes | yes | web ACLs | policies | done (policy sets go through change sets and revisions) | ctrl | S |
| Allowed-source list (Cloudflare ranges) | partial | partial | security groups | NSG | done (managed list) | HAProxy | |
| Network ACL / L3-L4 firewall policy | yes | AFM | NACL, SG | NSG | no (deny lists and the nftables rules for IPVS NAT only) | nftables | L |

### 2.6 Global and DNS load balancing

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Authoritative DNS with health-checked records | GSLB | GTM / DNS | Route 53 | Traffic Manager | done (PowerDNS zones and wide IPs, `ifportup`/`ifurlup` health, controller prober) | DNS | L |
| Priority (failover) routing | yes | global availability | failover | Priority | done (priority 1 to 10) | DNS | L |
| Weighted / ratio | yes | ratio | weighted | Weighted | done (weight 0 to 1000) | DNS | L |
| Geo / topology | yes | topology | geolocation | Geographic | done (geo views) | DNS | L |
| Performance / latency based | yes | RTT, QoS | latency | Performance | done (latency, measured from the controller only) | DNS | L |
| Multivalue answer | yes | yes | multivalue | Multivalue | done (`all` algorithm) | DNS | L |
| Subnet (client IP range) routing | yes | topology | IP-based | Subnet | done (subnet views) | DNS | L |
| DNSSEC | yes | yes | yes | no | done (`enable-dnssec` plus DS at the registrar) | DNS | L |
| LDNS persistence | yes | yes | no | no | no | DNS | L |
| DNS firewall / rate limit (dnsdist) | yes | DNS Express, DoS | Resolver FW | no | no | DNS | L |
| Link load balancing (multi-WAN) | yes | yes (LTM routing) | n/a | n/a | n/a: SEs are not edge routers | n/a | - |

### 2.7 Operations, observability, platform

| Capability | FortiADC | F5 | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| REST API, declarative config | yes | AS3 / iControl | API | ARM | done (202 paths, 276 operations; every write goes through a change set) | ctrl | |
| Config diff, rollback, audit, approvals | basic | basic | CloudTrail | Activity Log | done, ahead of most (rendered diff, canary, revisions, approvals, append-only audit) | ctrl | |
| Multi-tenancy with RBAC, quotas | VDOM / admin domains | partitions, route domains | accounts | subscriptions | done (RLS, OIDC/SAML, four roles, quotas) | ctrl | |
| Per-service metrics and logs | yes | yes | CloudWatch | Monitor | done (Prometheus scrape, per-VS readback through the API, Loki request logs) | ctrl+ext | **M** |
| Tenant-scoped dashboards (Grafana) | built in | Analytics | CloudWatch | Workbooks | done (one Grafana org and dashboard per tenant, data source pinned to a tenant-scoped proxy) | ext | **M** |
| Alerts and notifications | yes | yes | CloudWatch alarms | Monitor | part: rules over events, metrics and certificate expiry; webhook, SMTP and chat targets with HMAC pinning; no acknowledgement, silence, escalation or delivery retry | ctrl | **M** |
| Health score | yes | no | no | no | no | ctrl | S |
| Request logs with per-request fields | yes | yes | access logs | diagnostic logs | part: HAProxy httplog through Vector to Loki or OTLP, parsed into fields; no custom `log-format`, so no request id, SNI or upstream field per line | ext | **M** |
| Packet capture | yes | tcpdump | traffic mirroring | packet capture | done (agent `tcpdump`, capped at 60 s, 20k packets and 16 MiB) | ctrl | L |
| Auto scaling of ADC instances | yes (cloud) | yes (cloud) | automatic | automatic | part: scale-out policy with sustain and cooldown; scale-in is a manual single-engine retire | ctrl | S |
| Upgrade orchestration (rolling SE upgrade) | yes | yes | n/a | n/a | part: a roll replaces engines for a new template one at a time; no in-place software upgrade of an engine | ctrl | S |
| Backup / restore of controller | yes | UCS | n/a | n/a | done (pgBackRest under Patroni with a rehearsed PITR drill, plus an encrypted configuration export) | ctrl | **M** |
| Terraform / Ansible provider | yes | yes | yes | yes | done for OpenTofu (22 resources, 23 data sources, generated from the OpenAPI); no Ansible module | ctrl | S |
| Kubernetes ingress / Gateway controller | yes | CIS | AWS LB Controller | AGIC | done (Gateway API Gateways and HTTPRoutes, plus Services of type LoadBalancer with VIP writeback); no legacy Ingress | ctrl | L |
| Service discovery (VM tags, cloud APIs) | yes | yes | target groups | backend pools | done (Proxmox tags, 60 s reconcile) | ctrl | S |
| Importers from another vendor | no | no | no | no | done for Avi (virtual services, pools, monitors, certificates, policy sets); DataScripts and WAF are reported as manual | ctrl | L |
| Service insertion (GWLB / appliance chaining) | no | no | GWLB | Gateway LB | no | n/a | - |
| Global edge / CDN / anycast | no | no | CloudFront, Global Accelerator | Front Door | no | n/a | - |

### 2.8 Identity, zero trust and advanced security

Authentication in front of a service is where F5 APM, NetScaler AAA/Gateway (nFactor) and FortiADC auth profiles do their
most valuable work. Since revision 2 this is no longer a gap in Switchyard: an access policy set puts oauth2-proxy in
front of a route, a Dex broker supplies the identity providers, and the SE passes identity headers to the origin
(ADR 0195). What remains thin is session lifecycle and the administration console's own identity (see 6).

| Capability | FortiADC | F5 | NetScaler | AWS | Azure | Switchyard now | Fit | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| OIDC login in front of an HTTP service | yes | APM | AAA / nFactor | ALB OIDC | Entra, Front Door | done (access policy set plus the broker) | HAProxy + auth service | **M** |
| SAML SP login | yes | APM | AAA | via Cognito | Entra | done (the broker is a SAML SP) | auth service | S |
| LDAP / RADIUS / local users | yes | APM | AAA | no | no | done (broker providers; local users are the break-glass admin) | auth service | S |
| Multi-step conditional auth flows | basic | APM policy editor | nFactor | no | Conditional Access | part: `min_factors` step-up on a rule; no ordered multi-step flow editor | auth service | L |
| MFA (TOTP, push, WebAuthn) | yes | APM | nFactor | Cognito | Entra | delegated to the IdP; TOTP for the controller's break-glass admin | IdP | S |
| SSO to backends (identity headers, JWT, Kerberos delegation) | yes | APM | AAA / Gateway | ALB claims header | App Proxy | done for headers (user, groups, signed JWT; client copies dropped); no Kerberos delegation | auth service | **M** (headers), L (Kerberos) |
| Per-route authorization (group, claim, path) | yes | APM | AAA | ALB rules | no | done (groups, email domains, ID-token claims, `min_factors`) | ctrl + auth service | **M** |
| Sessions: cookie, idle and absolute timeout, logout, revoke | yes | APM | AAA | ALB | no | part: signed session cookie with idle and absolute expiry and logout through oauth2-proxy; no per-session revoke API | auth service | **M** |
| Client certificate auth (mTLS) | yes | yes | yes | ALB mTLS | App GW | done (ADR 0070) | HAProxy | |
| Certificate to identity mapping, OCSP for clients | yes | APM | AAA | no | no | part: CA, CRL and claims-based checks; the edge test is any-client-certificate and there is no CN/SAN to user mapping | HAProxy + ctrl | S |
| JWT validation for APIs | yes | yes | yes | API Gateway | APIM | done (`jwt_verify`, RS/PS/ES only, iss/aud/scope/exp, per-key PEM or synced JWKS) | HAProxy | S |
| Device posture / endpoint checks | no | APM | Gateway EPA | Verified Access | Conditional Access | done (sy-posture agent, step-ca device certificate, claims at the edge; ADR 0196) | ext | L |
| ZTNA or VPN gateway for non-HTTP apps | SSL VPN | APM | Gateway | Client VPN, Verified Access | Entra Private Access | no | ext | L |
| Origin lock-down (only the proxy reaches the app) | yes | yes | yes | security groups | NSG | done for managed ranges (Cloudflare list, nftables drop) | HAProxy + nft | |
| Rate limits on login paths, credential-stuffing controls | yes | yes | yes | WAF | WAF | done (rate limit policy sets plus login throttling) | HAProxy | S (with A5) |
| Bot management | yes | Bot Defense | Bot Mgmt | WAF Bot Control | Front Door bot rules | done (challenge, captcha, CrowdSec) | ext (CrowdSec, challenge) | L |
| DDoS (SYN, L4 floods, L7 floods) | yes | AFM / DoS | yes | Shield | DDoS Protection | part: L7 limits; L3/L4 is left upstream | nft + HAProxy | L |
| API security (schema validation, per-key quotas) | yes | AWAF API | API Security | API Gateway | APIM | part: schema ACLs (2.5) plus JWT scope checks; no per-key quota | ext | L |
| Audit of auth events | yes | yes | yes | CloudTrail | Monitor | part: the controller's audit catalog covers its own sign-ins, denials and revocations; data-plane login events reach the logs, not the audit table | ctrl | **M** |

### 2.9 NetScaler-specific items

| Capability | Note | Fit | Pri |
| --- | --- | --- | --- |
| Rewrite and responder policies (expressions, bind points, priorities) | Same job as A2 to A4, and built: a structured match and action model with an ordered request and response stage list, not an expression language (no free-form snippets). | HAProxy | M (A2 to A4) |
| Content switching virtual servers | Equals host and path routes to pools. Done. | done | |
| AppQoE, priority queueing | HAProxy queues, `maxconn`, `timeout queue`. `maxconn` and per-source limits are exposed; queue priority classes are not. | HAProxy | L |
| HTTP callouts (external decision per request) | HAProxy SPOE, the same sidecar pattern as WAF, CrowdSec and auth — all three ship; a user-defined callout does not. | ext | L |
| Gateway portal theming | Branding of the auth service login page. Not built. | ext | L |
| Clustering, SDX/MPX hardware tenancy | Replaced by SE groups and tenants. | n/a | - |
| Integrated caching, front-end optimization | See C5. Caching is built; front-end optimisation (minify, image resizing) is not. | HAProxy | L |

## 3. What the matrix says

1. **Core L4/L7 is at parity or ahead** on tenancy, audit, change control and IPv6. The gaps are not in routing or TLS.
2. **HTTP policy is no longer the distance.** Cookie, header and URI persistence, header and prefix rewrite, fixed
   responses, error pages, richer matches, weighted pools, rate limits and concurrency limits were the largest block in
   revision 2, and all shipped. What is left there is narrow: regex rewrite, a per-URL rate-limit
   key and regex health-check expectations.
3. **Observability is built; its workflow is not.** Metrics, per-VS readback, request logs, tenant-scoped Grafana and
   webhook/SMTP/chat delivery exist. Acknowledgement, silencing, escalation and delivery retry do not, so an on-call
   rotation cannot run on it yet.
4. **Data-path authentication shipped, which was the second big gap.** F5 APM, NetScaler AAA and FortiADC auth profiles
   now have an equivalent: OIDC/SAML/LDAP/RADIUS login per route, group and claim authorization, identity headers, JWT
   validation, mTLS and device posture. What is thinner is session lifecycle (no per-session revoke) and step-up flows.
5. **WAF is built** (Coraza SPOA with CRS, detect or block, fail mode chosen per policy). The cost that remains is the
   sidecar life cycle and false-positive tuning, not rendering. API body-schema validation is still missing.
6. **GSLB shipped** (PowerDNS zones, wide IPs, six algorithms, health, DNSSEC) but is single-site: one PowerDNS endpoint,
   health measured from the controller.
7. **Not worth chasing:** scripting engines (iRules and friends), SSL inspection, link load balancing, bridge mode,
   service insertion and CDN. They fight the "no free-form snippets" and "data plane independent of the controller" rules
   or need a different product.
8. **The real remaining distance is operating a fleet**, which this matrix only partly measures: no multi-site or remote
   engines, no in-place engine upgrade, no alert workflow, a controller upgrade that is still a hand-run CLI, and a
   console that accepts OIDC/SAML only. See 6.

## 4. Phased plan

Size: S = days (one MR), M = 1 to 2 weeks, L = several weeks. Each item that adds config surface needs an ADR, a golden
test, a field-rule entry and a UI form (the UI trails the backend by one step).

### 4.0 What has shipped since this plan

The plan below is kept as written. This table is the status at revision 3; "where" points at the code, so a reader can
check the claim instead of trusting it.

| Block | Status | Where |
| --- | --- | --- |
| A1 cookie, header and URI persistence | shipped | `domain/models.py` (`PersistenceCookie`, `PersistenceHeader`, `PersistenceUri`), `render/haproxy.py` |
| A2 header actions and URL rewrite | shipped (prefix rewrite only) | `RewritePolicySet`, `render/haproxy.py` |
| A3 richer route matches | shipped | `RouteMatch`, `domain/snapshot.py` |
| A4 fixed responses and error pages | shipped | `RespondPolicySet`, `ErrorPagesPolicySet` |
| A5 rate and connection limits | shipped (IP or header key) | `RateLimitPolicySet`, stick tables in `render/haproxy.py` |
| A6 member options, `backup`, timed drain, `send-proxy` | shipped | `PoolMember`, `services/members.py` (`expire_drains`) |
| A7 backend mTLS | shipped | `PoolMember` `crt`, bundle in `pipeline/bundles.py` |
| A8 health checks: body and header match, presets | shipped (literal match, no regex) | `HttpCheck`, `TcpPreset` |
| B1 telemetry pipeline and Grafana proxy | shipped | `services/observability.py`, `services/grafana.py`, `adapters/telemetry.py`, `se-image/files/vector.yaml` |
| B2 alerts (webhook, email, chat) | shipped, without acknowledgement or escalation | `services/alerts.py`, `adapters/notify.py` |
| B3 request-log view | shipped | `api/app.py` `/logs`, `ui/src/routes/t.$tenant._shell.logs.tsx` |
| B4 production image, Patroni, pgBackRest, SE image, runbooks | shipped | `deploy/prod/`, `deploy/helm/`, `se-image/`, `docs/runbooks/` |
| B5 live checks (`.p12`, conflict view, lab) | shipped | `deploy/lab/checks/live.sh`, `ui/src/features/review/ConflictResolver.tsx` |
| C1 HTTP/3, gRPC and WebSocket | HTTP/3 shipped; gRPC and WebSocket are `backend_protocol` plus `tunnel_timeout_s`, without a health check | `VirtualService` `http3`, `Pool.backend_protocol` |
| C2 weighted pools and priority chains | shipped | `WeightedPool`, the route pool chain in `render/haproxy.py` |
| C3 persistence sync and SNAT | `peers` shipped; SNAT not exposed | `PersistenceSourceIp` `peers` |
| C4 TLS passthrough with SNI routing | shipped | the passthrough flag on a TCP service, `render/haproxy.py` |
| C5 compression and cache | shipped | `Compression`, `CacheSettings` |
| C6 Coraza WAF | shipped | `WafPolicySet`, `render/waf.py`, `se-image/files/coraza-spoa.service` |
| C7 ACME HTTP-01 and TLS-ALPN-01, expiry alerts | shipped | `services/http01.py`, `services/tlsalpn.py`, `services/alerts.py` |
| C9 pool discovery, health score, rolling upgrade, scale-out | discovery and scale-out shipped; health score not built; an upgrade is a template roll | `services/discovery.py`, `services/autoscale.py`, `services/roll.py` |
| C10 OpenTofu provider | shipped | `provider/internal/provider/zz_generated.go` |
| C11 geo and reputation lists | shipped | `services/iplists.py`, `render/haproxy.py` |
| E1 auth service in front of a service | shipped | `render/auth.py`, `services/broker.py`, the se-image `sy-auth` unit |
| E2 per-route authorization | shipped | `OidcSettings`, claim checks in `render/haproxy.py` |
| E3 identity headers to the origin | shipped (no Kerberos) | `render/auth.py`, `render/haproxy.py` |
| E4 auth events in audit and logs | part: logs yes, audit table no | the Vector pipeline, `services/sessions.py` |
| E5 SAML, LDAP/RADIUS and local providers | shipped in the broker | `services/broker.py`, `services/idp.py` |
| E6 client certificate identity | part: CA, CRL and claims; no CN/SAN mapping | `domain/posture.py`, `services/devices.py` |
| E7 JWT validation for APIs | shipped | `render/tokens.py`, `services/jwks_sync.py` |
| E8 conditional step-up flows | part: `min_factors` only | `OidcSettings` |
| E9 device posture and ZTNA | posture shipped; ZTNA not built | `domain/posture.py`, `services/devices.py` |
| E10 bot defense, DDoS, login rate limits, API quotas | bot and login limits shipped; DDoS is per-source limits; per-key API quota not built | `render/captcha.py`, `render/crowdsec.py` |
| D1 GSLB on PowerDNS | shipped, single site | `domain/gslb.py`, `services/gslb.py`, `adapters/powerdns.py` |
| D2 Kubernetes connector | shipped (Gateway API plus LoadBalancer Services) | `connectors/kubernetes.py` |
| D3 Avi importer | shipped | `importers/avi.py` |
| D5 API schema protection, packet capture | both shipped (no body schema) | `render/apischema.py`, `services/captures.py` |
| D6 controlled Lua | not built beyond the generated challenge script | `render/captcha.py` |
| D7 IPVS DR and tunnel modes | shipped | `render/routing.py` (ADR 0192) |
| D8 tenant self-service portal | shipped (usage, quotas, quota requests) | `ui/src/routes/t.$tenant._shell.portal.tsx` |

### Phase A: HTTP policy parity (must)

| # | Item | Size | Depends on | Notes |
| --- | --- | --- | --- | --- |
| A1 | Cookie persistence (insert, rewrite, prefix) with TTL; persistence on header and URI | M | none | Shipped. |
| A2 | Request/response header actions (set, add, remove, with a safe variable list) and URL rewrite | M | none | Shipped, prefix rewrite only. |
| A3 | Richer route matches: method, header, query, source CIDR, SNI | M | A2 (shares the condition model) | Shipped. |
| A4 | Fixed responses and custom error pages | S | A2 | Shipped. |
| A5 | Rate limits and connection limits (per source, per path), tarpit/deny | M | A3 | Shipped with an IP or header key; a per-path key is still open. |
| A6 | Member options: `backup`, per-member `maxconn`, timed drain; `send-proxy` to backends | S | none | Shipped. |
| A7 | Backend mTLS (client cert on server lines) | S | ADR 0070 | Shipped. |
| A8 | Health checks: HTTPS body/header match, `tcp-check` send/expect presets (Redis, SMTP, LDAP, MySQL) | M | none | Shipped; regex expectations are still open. |

### Phase B: Operate it (must)

| # | Item | Size | Depends on | Notes |
| --- | --- | --- | --- | --- |
| B1 | Telemetry pipeline live (exporter, Prometheus, Vector, Loki) and tenant-scoped Grafana proxy | L | running threads | Shipped. |
| B2 | Alerts: outbound webhook, email and chat targets per tenant; rules on events and metrics | M | B1 | Delivery shipped; acknowledgement, silence, escalation and retry are open (see 6). |
| B3 | Request-log view in the UI (Loki query behind the API) | M | B1 | Shipped. |
| B4 | Controller image, production Compose, Patroni, pgBackRest, SE image recipe, runbooks | L | Operations thread | Shipped. |
| B5 | Live checks: `.p12`, conflict view, end-to-end lab in CI | M | B4 | Shipped. |

### Phase C: Differentiators that stay on-architecture (should)

| # | Item | Size | Depends on | Notes |
| --- | --- | --- | --- | --- |
| C1 | HTTP/3 on ports, gRPC and WebSocket as explicit options, with checks and tests | M | none | HTTP/3 shipped and lab-tested; gRPC and WebSocket are the backend protocol plus the tunnel timeout, with no health check and no upgrade test. |
| C2 | Weighted pool routing (canary, blue-green) and pool priority groups | M | A3 | Shipped. |
| C3 | Persistence table sync (`peers`) and SNAT options | M | A1 | `peers` shipped; SNAT pools are not exposed. |
| C4 | TLS passthrough with SNI routing on TCP services | S | none | Shipped. |
| C5 | Compression; then cache | S, M | A2 | Shipped. |
| C6 | Coraza WAF: `waf_policy`, SPOA sidecar in the SE image, detect-first, CRS version pinned | L | B1 (logs), B4 (image) | Shipped. |
| C7 | ACME HTTP-01 and TLS-ALPN-01; certificate expiry notifications | M | B2 | Shipped. |
| C8 | Moved to phase E (E1). | | | |
| C9 | Pool discovery by Proxmox tags; health score; rolling SE upgrade job; scale-out | L | B4 | Discovery and scale-out shipped; health score not built; a roll replaces engines rather than upgrading them in place. |
| C10 | OpenTofu provider generated from the OpenAPI | M | none | Shipped. |
| C11 | Geo and IP-reputation lists (managed lists like the Cloudflare one, MaxMind, CrowdSec bouncer) | M | A5 | Shipped. |

### Phase E: Identity-aware access and zero trust (E1 to E4 must, rest should or later)

Architecture: an **auth service** per SE group (a sidecar in the SE image, for example oauth2-proxy or a small Go
service). HAProxy calls it for each request on a protected route (SPOE or an auth subrequest; 01 already names this).
Sessions are signed cookies, so the SE needs no shared store. The controller owns the config (tenant IdP, route policy);
the SE keeps serving existing sessions if the controller is down; secrets are `secret_ref`s into OpenBao.

| # | Item | Size | Depends on | Notes |
| --- | --- | --- | --- | --- |
| E1 | Auth service in the SE image: OIDC login, session cookie, logout, idle and absolute timeouts. ADR for the call contract and fail mode (closed by default) | L | A3, B4 | Shipped. |
| E2 | Per-route authorization: require an IdP group, claim or email-domain rule, per route or path | M | E1 | Shipped. |
| E3 | Identity headers to the origin (user, groups, signed JWT); client-supplied copies dropped | S | E1, A2 | Shipped. |
| E4 | Auth events in audit and logs (login, deny, logout, revoke), tenant-scoped | S | E1, B1 | Logs yes; the audit table does not carry data-plane logins yet. |
| E5 | SAML SP, LDAP/RADIUS and local-user providers | M each | E1 | Shipped in the broker. |
| E6 | Client certificate identity: map CN/SAN to a user or group, OCSP for client certs, certificate plus OIDC as two factors | M | E1, ADR 0070 | Part: CA, CRL and claims; no CN/SAN mapping and no client OCSP. |
| E7 | JWT validation for APIs (issuer, audience, scopes), no login page | M | A3 | Shipped. |
| E8 | Conditional flows (nFactor-style step-up by path, group or network) | L | E1 | Part: `min_factors` on a rule. |
| E9 | Device posture and a ZTNA gateway for non-HTTP apps | L | E1 | Posture shipped; ZTNA not built. |
| E10 | Bot defense and DDoS profiles, login-path rate limits, API quotas per key | L | A5, C6 | Bot and login limits shipped; DDoS is per-source limits; per-key API quota not built. |

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
- **Engine upgrades are replacements.** A roll swaps an engine for a new template instead of upgrading it in place, and
  it needs an autoscale deploy and enroll policy plus a guest agent. A CVE in HAProxy, Coraza or the Data Plane API
  therefore means rebuilding engines one at a time. Decide whether an in-place upgrade path is worth the agent surface
  before a security advisory forces the question.
- **Alerting has no human workflow.** Delivery exists; acknowledgement, silence, maintenance windows, escalation and
  retry do not. Do not promise an on-call rotation until they do.
- **Source quality.** Some vendor comparisons above (HTTP/3 on ALB, per-method availability on Front Door) change often.
  Confirm against the current vendor page when an item is picked up.

## 6. Operational gaps this matrix does not measure

The matrix compares features a datasheet lists. These decide whether a fleet can be run, and none of them is a missing
HAProxy feature. Ordered by the cost of not having it.

1. **Multi-site and remote engines** (ADR 0180, proposed): no `site` object, the controller dials engines inbound on 7443
   and 5556, and deployment is Proxmox-only. An engine behind NAT or in another cloud cannot be managed at all.
2. **No in-place engine upgrade**: a roll replaces VMs, scale-in is a manual retire, and `services/roll_actions.py` says
   it is not yet exercised against a real cluster.
3. **No alerting workflow**: no acknowledgement, silence, escalation or delivery retry, and no Alertmanager compatibility.
4. **The controller upgrade is a hand-run CLI** (`syctl upgrade apply` per node, marked not yet exercised on a real node),
   with no cross-member ordering and no Helm path.
5. **Console identity**: OIDC and SAML only (no LDAP bind, RADIUS or TACACS+), and no client-certificate authentication to
   the API. An estate standardised on AD/LDAP or smartcard mTLS needs a bridge first.
6. **Observability depth**: HAProxy uses the default httplog (no request id, SNI or upstream field), the controller's own
   logs and the audit table are not shipped anywhere, controller metrics are fleet-wide rather than per tenant, and the
   chart ships no ServiceMonitor or alert rules.
7. **Lifecycle detail**: a running job cannot be cancelled (only a queued one), the audit table has no retention or SIEM
   forward, backups are scheduled by host cron with no automated verify or staleness alert, VS-derived DNS records work
   on Cloudflare only, and the Git mirror is a push-only GitLab mirror rather than a GitOps source.
8. **Data-plane model holes**: no gRPC or HTTP/2 health check, no
   regex health-check expectation, no per-URL rate-limit key, no JSON body schema validation, and WebSocket support is
   asserted but untested.

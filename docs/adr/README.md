# Architecture decision records

One file per decision that is expensive to reverse. Use the next free number, keep the filename
`NNNN-short-title.md`, and never rewrite an accepted record: add a new one that supersedes it and
change the old record's status line.

Template:

```markdown
# NNNN. Title

Status: Proposed | Accepted | Superseded by NNNN
Date: YYYY-MM-DD

## Context
## Decision
## Consequences
## Alternatives considered
```

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-layering.md) | Layering: api → services → domain, with pure `domain` and `render` | Accepted |
| [0002](0002-tenancy-rls.md) | Tenancy is enforced in the service layer and again by PostgreSQL RLS | Accepted |
| [0003](0003-change-op-model.md) | Change operations: the model is fixed now, the code arrives in M2 | Accepted |
| [0004](0004-renderer-determinism-and-safety.md) | The renderer is deterministic, validated by real binaries, and rejects unsafe values | Accepted |
| [0005](0005-schema-and-id-conventions.md) | Schema conventions: UUIDv7, `version`, composite tenant foreign keys, text enums | Accepted |
| [0006](0006-http-routing-semantics.md) | HTTP routing order, allow lists and drain semantics | Accepted |
| [0007](0007-change-pipeline-and-engine-contract.md) | Change pipeline and the engine contract | Accepted |
| [0008](0008-agent-routing-and-status.md) | The SE agent, routing profiles, and status/drift | Accepted |
| [0009](0009-infrastructure-jobs.md) | Infrastructure jobs: SDN, IPAM and SE group deployment | Accepted |
| [0010](0010-certificates-and-cloudflare.md) | Certificates, OpenBao and the Cloudflare allow list | Accepted |
| [0011](0011-agent-enrollment.md) | Agent enrollment and certificate renewal | Accepted |
| [0012](0012-user-login-and-sessions.md) | User login, sessions and per-tenant roles | Accepted |
| [0013](0013-udp-keepalived-ipvs.md) | UDP via Keepalived/IPVS: forwarding mode and scope | Accepted |
| [0014](0014-typed-api-contract.md) | Typed API contract: response models, `kind` unions, cursors, diff warnings | Accepted |
| [0015](0015-rendered-config-per-object.md) | Rendered configuration per object, and typed diff files | Accepted |
| [0016](0016-job-model-stages-and-actions.md) | Job model: stages, log, retry and cancel | Accepted |
| [0017](0017-sdn-cleanup.md) | Proxmox SDN cleanup when a managed network is deleted | Accepted |
| [0018](0018-saml-login.md) | SAML 2.0 login (signxml, signed assertions, replay cache, SP metadata) | Accepted |
| [0020](0020-udp-m6-implementation.md) | UDP (M6) implementation: monitor, field rules, bundle files, IPVS status, watchdog | Accepted |
| [0030](0030-rbac-matrix-and-access-lifecycle.md) | Permission matrix, multi-group grants, role refresh, users and token scope | Accepted |
| [0031](0031-access-refresh-users-and-codes.md) | How access refresh, users and error codes are implemented | Accepted |
| [0032](0032-idp-admin-test-signin-break-glass.md) | Identity-provider administration, test sign-in, break-glass enrolment, system tokens | Accepted |
| [0040](0040-api-completeness-for-the-ui.md) | API completeness for the UI: audit, IPAM, health, telemetry, events, documentation rules | Accepted |
| [0041](0041-agent-member-probe-and-withdrawn-prefixes.md) | Agent member probe (health monitor test) and the withdrawn prefix list | Accepted |
| [0042](0042-ui-theme-and-fonts.md) | UI theme: shadcn tokens, status tokens and fonts | Accepted |
| [0043](0043-pkcs12-certificate-import.md) | PKCS#12 certificate import | Accepted |
| [0044](0044-replace-certificate-in-place.md) | Replace a certificate in place | Accepted |
| [0045](0045-tenant-quota-enforcement.md) | Tenant quota enforcement and usage counts | Accepted |
| [0046](0046-revision-message.md) | Revision message on apply | Accepted |
| [0047](0047-member-health-detail.md) | Per-member health and last check | Accepted |
| [0048](0048-production-deployment-shape.md) | Production deployment shape: controller image, Patroni, pgBackRest | Accepted |
| [0049](0049-proxmox-host-state-and-placement.md) | Proxmox host state and placement read | Accepted |
| [0050](0050-se-image-recipe.md) | SE image recipe | Accepted |
| [0051](0051-management-tenant-lock-and-adopt.md) | Management tenant lock and the `adopt` import | Accepted |
| [0052](0052-telemetry-pipeline.md) | Telemetry pipeline: Prometheus, Loki, Grafana, Vector | Accepted |
| [0053](0053-ui-proxy-and-headers.md) | UI proxy, security headers and login rate limiting | Accepted |
| [0054](0054-proxmox-live-adapter-tests.md) | Live Proxmox adapter tests with dockur/proxmox | Accepted |
| [0055](0055-guest-kind-vm-or-lxc.md) | Service engines as VMs or LXC containers | Accepted |
| [0056](0056-end-to-end-lab.md) | End-to-end lab on a real Proxmox | Accepted |
| [0057](0057-data-nic-addressing.md) | Addresses for the VIP and backend NICs of a deployed engine | Accepted |
| [0058](0058-quickstart-and-production-guide.md) | Quickstart stack and production guide | Accepted |
| [0060](0060-audit-export.md) | Audit export endpoint | Accepted |
| [0061](0061-controller-metrics-and-sd.md) | Controller metrics and Prometheus service discovery | Accepted |
| [0062](0062-principal-display-names.md) | Display names for principal labels | Accepted |
| [0063](0063-haproxy-exporter-listener.md) | HAProxy Prometheus exporter listener on the management network | Accepted |
| [0064](0064-certificate-renew-before-days.md) | Certificate renewal window (`renew_before_days`) | Accepted |
| [0065](0065-bgp-prefix-limits.md) | BGP prefix limits | Accepted |
| [0066](0066-pool-passive-checks-and-slow-start.md) | Pool passive checks and slow start | Accepted |
| [0067](0067-tls-profiles-and-ocsp.md) | TLS profiles and OCSP stapling | Accepted |
| [0068](0068-ui-support-fields.md) | Small fields the UI asked for | Accepted |
| [0069](0069-drift-reapply.md) | Re-apply the desired state to an SE group | Accepted |
| [0070](0070-client-mtls.md) | Client certificate verification (mutual TLS) | Accepted |
| [0071](0071-secret-reference-scope.md) | Secret references stay inside the tenant | Accepted |
| [0072](0072-hardening-engines-logins-secrets.md) | Hardening: engine addresses, login throttling, secret namespaces | Accepted |
| [0073](0073-engine-config-views.md) | Rendered config per engine and running config of an engine | Accepted |
| [0074](0074-agent-bundle-binding.md) | Bundles bound to an engine and a sequence; named callers for the agent | Accepted |
| [0075](0075-deploy-hardening.md) | Deploy hardening: templates, node names, engine addresses | Accepted |
| [0076](0076-lxc-guests-setting.md) | A system setting for container (LXC) guests | Accepted |
| [0077](0077-wait-for-storage-reloads.md) | Wait for the reload a storage call starts | Accepted |
| [0078](0078-ipv6-and-dual-stack.md) | IPv6 and dual-stack: VRRP per family, derived BGP router id | Accepted |
| [0079](0079-two-host-lab-findings.md) | What the two real Proxmox hosts changed | Accepted |
| [0080](0080-acme-providers-and-dns-credentials.md) | ACME providers, DNS credentials and HTTP-01 | Accepted |
| [0081](0081-controller-address-settings.md) | Changing the controller's address, VIP and certificate | Accepted |
| [0082](0082-tls-alpn-01.md) | TLS-ALPN-01 challenges | Accepted |
| [0083](0083-se-image-sidecar-slot-and-rolling-operations.md) | SE image sidecar slot, pinned versions, rolling upgrade, health score | Accepted |
| [0085](0085-grafana-behind-controller.md) | Grafana behind the controller, scoped per tenant: every data source query rewritten to one tenant | Accepted |
| [0100](0100-policy-model-and-request-stages.md) | Policy model: one condition type and fixed request stages | Accepted |
| [0101](0101-route-match-model.md) | Route and rule matches: method, header, query, source network and SNI | Accepted |
| [0102](0102-header-and-url-rewrite.md) | Header and URL rewrite policy sets | Accepted |
| [0103](0103-fixed-responses-and-error-pages.md) | Fixed responses and custom error pages | Accepted |
| [0105](0105-pool-persistence-kinds.md) | Pool persistence: cookie, header and URI | Accepted |
| [0106](0106-member-options-and-send-proxy.md) | Member backup and maxconn, PROXY protocol to members | Accepted |
| [0107](0107-backend-client-certificate.md) | Client certificate to pool members (backend mTLS) | Accepted |
| [0108](0108-health-check-presets.md) | Health checks: body and header match, protocol presets | Accepted |
| [0109](0109-timed-member-drain.md) | Timed member drain | Accepted |
| [0110](0110-rate-and-concurrency-limits.md) | Rate and concurrency limits | Accepted |
| [0115](0115-sni-passthrough.md) | TLS passthrough with SNI routing on TCP services | Accepted |
| [0116](0116-http3-grpc-websocket.md) | HTTP/3, gRPC backends and WebSocket timeout | Accepted |
| [0117](0117-response-compression.md) | Response compression | Accepted |
| [0118](0118-persistence-peers.md) | Persistence table sync between engines (peers) | Accepted |
| [0119](0119-weighted-pool-routes.md) | Weighted pool routes (canary and blue-green) | Accepted |
| [0120](0120-alerting-model.md) | Alerting: tenant targets and rules, controller-side evaluator | Accepted |
| [0121](0121-alert-senders-and-destinations.md) | Alert senders: webhook, chat, email; destination policy and signing | Accepted |
| [0122](0122-certificate-expiry-notifications.md) | Certificate expiry notifications | Accepted |
| [0123](0123-tenant-log-api.md) | Tenant-wide request log API | Accepted |
| [0130](0130-auth-service-architecture.md) | Authentication service: oauth2-proxy, loopback re-entry, closed by default | Accepted |
| [0131](0131-access-rules-and-authorization.md) | Access rules and authorization | Accepted |
| [0132](0132-identity-headers.md) | Identity headers to the origin | Accepted |
| [0133](0133-auth-events.md) | Auth events: what is logged and where it goes | Accepted |
| [0135](0135-jwt-validation-for-apis.md) | JWT validation for APIs | Accepted |
| [0136](0136-client-certificate-identity.md) | Client certificate identity | Accepted |
| [0137](0137-directory-logins-saml-ldap-local.md) | Directory logins (SAML, LDAP, local users) through a central login broker | Accepted |
| [0138](0138-device-posture-signals.md) | Device posture signals in access rules | Accepted |
| [0140](0140-waf-policy-set.md) | WAF policy set | Accepted |
| [0141](0141-waf-detect-first-and-crs-pinning.md) | Detect first, and a pinned Core Rule Set | Accepted |
| [0142](0142-waf-sidecar-and-failure-mode.md) | WAF sidecar life cycle and failure mode | Accepted |
| [0143](0143-waf-logs.md) | WAF logs in Loki | Accepted |
| [0144](0144-waf-image-and-agent-contract.md) | What the SE image and the agent must provide for the WAF | Accepted |
| [0145](0145-pool-discovery-by-proxmox-tag.md) | Pool discovery by Proxmox tag | Accepted |
| [0146](0146-opentofu-provider-from-the-openapi.md) | OpenTofu provider generated from the OpenAPI document | Accepted |
| [0147](0147-country-and-reputation-ip-lists.md) | Country and reputation IP lists | Accepted |
| [0150](0150-avi-importer.md) | Avi configuration importer | Accepted |
| [0151](0151-kubernetes-connector.md) | Kubernetes connector | Accepted |
| [0152](0152-crs-rule-catalogue.md) | OWASP CRS rule catalogue for the WAF screen | Accepted |
| [0153](0153-jwks-url.md) | JWKS URL on JWT rules, refreshed by the controller | Accepted |
| [0154](0154-v53-read-additions.md) | WAF flag on log lines, Gateway API checks, latency in the wide IP read | Accepted |
| [0155](0155-management-tenant-at-startup.md) | Management tenant created at start-up; an IP can be the public address | Accepted |
| [0160](0160-gslb-on-powerdns.md) | GSLB on PowerDNS: the controller decides, PowerDNS answers and health-checks | Accepted |
| [0161](0161-gslb-algorithms-and-record-safety.md) | GSLB algorithms, health checks and keeping tenant input out of Lua | Accepted |
| [0162](0162-gslb-zones-ownership-and-dnssec.md) | GSLB zones: ownership, DNSSEC, several sites | Accepted |
| [0163](0163-gslb-scope-and-deferred.md) | GSLB: what is built and what is not | Accepted |
| [0164](0164-dnsdist-in-front-of-powerdns.md) | dnsdist in front of PowerDNS: ANY and transfers refused, per-network rate limits | Accepted |
| [0165](0165-gslb-latency-selection.md) | GSLB latency-based selection: measured round trips with hysteresis | Accepted |
| [0170](0170-bot-and-ddos-protection.md) | Bot and DDoS protection set | Accepted |
| [0171](0171-javascript-challenge.md) | JavaScript challenge (proof of work) | Accepted |
| [0172](0172-captcha-gate.md) | CAPTCHA gate with a pluggable provider | Accepted |
| [0180](0180-multi-site-and-remote-engines.md) | Multi-site: sites, remote engines over an outbound channel, per-site DNS and probes | Proposed |
| [0190](0190-docs-site-mkdocs-material.md) | Docs site: MkDocs with the Material theme | Accepted |
| [0191](0191-kubernetes-deployment.md) | Kubernetes is a supported place to run the controller (Helm chart) | Accepted |
| [0192](0192-ipvs-direct-return-and-tunnel.md) | IPVS direct-return and tunnel forwarding for UDP services | Accepted |
| [0194](0194-autoscale-and-packet-capture.md) | Automatic scale-out of SE groups, and packet capture on engines | Accepted |
| [0195](0195-radius-login-and-step-up-flows.md) | RADIUS logins, a login gate, and multi-step (nFactor style) flows | Accepted |
| [0196](0196-device-posture-agent-and-device-certificates.md) | Device posture from an agent, short-lived device certificates, and a TCP tunnel | Accepted |
| [0197](0197-waf-api-schema-protection.md) | API schema protection in the WAF policy | Accepted |
| [0198](0198-object-labels.md) | Object labels on virtual services and pools, and the `?label=` filter | Accepted |
| [0199](0199-tenant-portal-and-quota-requests.md) | Tenant portal: usage, limits and quota requests | Accepted |
| [0200](0200-sbom-and-reuse-licensing.md) | SBOMs (Syft, SPDX) in CI and REUSE licensing, Apache-2.0 default | Accepted |
| [0205](0205-upgrades-versions-bundles-backups.md) | Upgrades: version compatibility, upgrade bundles, backups, checks, canary rollout | Accepted |
| [0206](0206-nightly-and-release-builds.md) | Nightly builds, releases and CI on GitLab and GitHub | Accepted |
| [0208](0208-github-front-door.md) | GitHub as the front door, GitLab as the merge point: pull request bridge, DCO | Accepted |
| [0210](0210-security-review-2026-10-13.md) | Fixes from the 2026-10-13 security review | Accepted |

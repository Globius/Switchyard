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
| [0020](0020-udp-m6-implementation.md) | UDP (M6) implementation: monitor, field rules, bundle files, IPVS status, watchdog | Accepted |
| [0030](0030-rbac-matrix-and-access-lifecycle.md) | Permission matrix, multi-group grants, role refresh, users and token scope | Accepted |
| [0031](0031-access-refresh-users-and-codes.md) | How access refresh, users and error codes are implemented | Accepted |
| [0032](0032-idp-admin-test-signin-break-glass.md) | Identity-provider administration, test sign-in, break-glass enrolment, system tokens | Accepted |
| [0040](0040-api-completeness-for-the-ui.md) | API completeness for the UI: audit, IPAM, health, telemetry, events, documentation rules | Accepted |
| [0041](0041-agent-member-probe-and-withdrawn-prefixes.md) | Agent member probe (health monitor test) and the withdrawn prefix list | Accepted |
| [0018](0018-saml-login.md) | SAML 2.0 login (signxml, signed assertions, replay cache, SP metadata) | Accepted |
| [0042](0042-ui-theme-and-fonts.md) | UI theme: shadcn tokens, status tokens and fonts | Accepted |
| [0060](0060-audit-export.md) | Audit export endpoint | Accepted |
| [0049](0049-proxmox-host-state-and-placement.md) | Proxmox host state and placement read | Accepted |
| [0047](0047-member-health-detail.md) | Per-member health and last check | Accepted |
| [0046](0046-revision-message.md) | Revision message on apply | Accepted |
| [0045](0045-tenant-quota-enforcement.md) | Tenant quota enforcement and usage counts | Accepted |
| [0062](0062-principal-display-names.md) | Display names for principal labels | Accepted |
| [0061](0061-controller-metrics-and-sd.md) | Controller metrics and Prometheus service discovery | Accepted |
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
| [0105](0105-pool-persistence-kinds.md) | Pool persistence: cookie, header and URI | Accepted |
| [0106](0106-member-options-and-send-proxy.md) | Member backup and maxconn, PROXY protocol to members | Accepted |
| [0107](0107-backend-client-certificate.md) | Client certificate to pool members (backend mTLS) | Accepted |
| [0108](0108-health-check-presets.md) | Health checks: body and header match, protocol presets | Accepted |
| [0109](0109-timed-member-drain.md) | Timed member drain | Accepted |
| [0115](0115-sni-passthrough.md) | TLS passthrough with SNI routing on TCP services | Accepted |
| [0116](0116-http3-grpc-websocket.md) | HTTP/3, gRPC backends and WebSocket timeout | Accepted |
| [0117](0117-response-compression.md) | Response compression | Accepted |
| [0118](0118-persistence-peers.md) | Persistence table sync between engines (peers) | Accepted |
| [0119](0119-weighted-pool-routes.md) | Weighted pool routes (canary and blue-green) | Accepted |
| [0145](0145-pool-discovery-by-proxmox-tag.md) | Pool discovery by Proxmox tag | Accepted |
| [0146](0146-opentofu-provider-from-the-openapi.md) | OpenTofu provider generated from the OpenAPI document | Accepted |
| [0145](0145-pool-discovery-by-proxmox-tag.md) | Pool discovery by Proxmox tag | Accepted |
| [0146](0146-opentofu-provider-from-the-openapi.md) | OpenTofu provider generated from the OpenAPI document | Accepted |
| [0147](0147-country-and-reputation-ip-lists.md) | Country and reputation IP lists | Accepted |
| [0120](0120-alerting-model.md) | Alerting: tenant targets and rules, controller-side evaluator | Accepted |
| [0121](0121-alert-senders-and-destinations.md) | Alert senders: webhook, chat, email; destination policy and signing | Accepted |
| [0122](0122-certificate-expiry-notifications.md) | Certificate expiry notifications | Accepted |
| [0123](0123-tenant-log-api.md) | Tenant-wide request log API | Accepted |
| [0085](0085-grafana-behind-controller.md) | Grafana behind the controller, scoped per tenant: every data source query rewritten to one tenant | Accepted |
| [0080](0080-acme-providers-and-dns-credentials.md) | ACME providers, DNS credentials and HTTP-01 | Accepted |
| [0100](0100-policy-model-and-request-stages.md) | Policy model: one condition type and fixed request stages | Accepted |
| [0101](0101-route-match-model.md) | Route and rule matches: method, header, query, source network and SNI | Accepted |
| [0102](0102-header-and-url-rewrite.md) | Header and URL rewrite policy sets | Accepted |
| [0103](0103-fixed-responses-and-error-pages.md) | Fixed responses and custom error pages | Accepted |
| [0110](0110-rate-and-concurrency-limits.md) | Rate and concurrency limits | Accepted |
| [0130](0130-auth-service-architecture.md) | Authentication service: oauth2-proxy, loopback re-entry, closed by default | Accepted |
| [0131](0131-access-rules-and-authorization.md) | Access rules and authorization | Accepted |
| [0132](0132-identity-headers.md) | Identity headers to the origin | Accepted |
| [0133](0133-auth-events.md) | Auth events: what is logged and where it goes | Accepted |
| [0135](0135-jwt-validation-for-apis.md) | JWT validation for APIs | Accepted |
| [0136](0136-client-certificate-identity.md) | Client certificate identity | Accepted |
| [0137](0137-directory-logins-saml-ldap-local.md) | Directory logins (SAML, LDAP, local users) through a central login broker | Accepted |
| [0138](0138-device-posture-signals.md) | Device posture signals in access rules | Accepted |

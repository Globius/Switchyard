# Why it works this way

Short answers to "why did you…?", each pointing at the decision record that settles it. The full list is the
[ADR index](adr/README.md).

## Shape of the system

| Question | Answer | ADR |
| --- | --- | --- |
| Why can't the API code call PostgreSQL or Proxmox directly from anywhere? | Layers depend downward only; `domain` and `render` stay pure and testable | [0001](adr/0001-layering.md) |
| Why is tenancy checked twice? | Service-layer checks plus PostgreSQL RLS, so one bug does not leak a tenant | [0002](adr/0002-tenancy-rls.md) |
| Why UUIDv7, versions and composite foreign keys? | Sortable ids, optimistic concurrency, references that cannot cross tenants | [0005](adr/0005-schema-and-id-conventions.md) |
| Why change sets instead of direct edits? | One validated, reviewable, revertible unit per change | [0003](adr/0003-change-op-model.md), [0007](adr/0007-change-pipeline-and-engine-contract.md) |
| Why a canary engine on every apply? | A bad config hits one engine, never the whole group | [0007](adr/0007-change-pipeline-and-engine-contract.md) |
| Why is drift reported but not fixed? | Silent overwrites hide incidents; re-apply is an explicit act | [0008](adr/0008-agent-routing-and-status.md), [0069](adr/0069-drift-reapply.md) |
| Why must the renderer be deterministic? | Same state, same bytes: fingerprints, drift detection and golden tests depend on it | [0004](adr/0004-renderer-determinism-and-safety.md) |
| Why no free-form config snippets? | Every value must pass a validator and escaping; snippets would bypass both | [0004](adr/0004-renderer-determinism-and-safety.md), [0100](adr/0100-policy-model-and-request-stages.md) |
| Why is the job queue in PostgreSQL and not Redis? | One less system to run and secure; `SKIP LOCKED` is enough | [0009](adr/0009-infrastructure-jobs.md), [0016](adr/0016-job-model-stages-and-actions.md) |
| Why a typed API contract checked in CI? | The UI client and the OpenTofu provider are generated from it | [0014](adr/0014-typed-api-contract.md), [0146](adr/0146-opentofu-provider-from-the-openapi.md) |

## Engines and the data plane

| Question | Answer | ADR |
| --- | --- | --- |
| Why HAProxy, FRR and Keepalived? | Mature, open-source, and each does one job: L7/L4, BGP, VRRP and IPVS | [replacement plan](design/01-replacement-plan.md) |
| Why an agent at all, and why so limited? | Some files are not HAProxy's; the agent writes only under `/etc/switchyard/` and has no shell | [0008](adr/0008-agent-routing-and-status.md), [0011](adr/0011-agent-enrollment.md) |
| Why are bundles bound to an engine and a sequence? | No replay and no redirection of a signed bundle | [0074](adr/0074-agent-bundle-binding.md) |
| Why IPVS NAT for UDP and not direct routing? | Backends need no change; tenants' backends are not ours to configure | [0013](adr/0013-udp-keepalived-ipvs.md) |
| Why VMs by default and LXC behind a setting? | Containers share the host kernel | [0055](adr/0055-guest-kind-vm-or-lxc.md), [0076](adr/0076-lxc-guests-setting.md) |
| Why only Proxmox templates can be cloned? | So a deploy cannot copy another tenant's running engine | [0075](adr/0075-deploy-hardening.md) |
| Why are engines replaced instead of upgraded in place? | Disposable engines and a health gate make upgrades routine | [0050](adr/0050-se-image-recipe.md), [0083](adr/0083-se-image-sidecar-slot-and-rolling-operations.md) |
| Why no external health-check scripts? | They would run commands on engines | [0066](adr/0066-pool-passive-checks-and-slow-start.md) |

## Security features

| Question | Answer | ADR |
| --- | --- | --- |
| Why oauth2-proxy as a loopback hop and not Lua or SPOE? | No custom code in the request path; fails closed | [0130](adr/0130-auth-service-architecture.md) |
| Why does the WAF start in detect mode? | Tenants tune false positives before anything is blocked | [0141](adr/0141-waf-detect-first-and-crs-pinning.md) |
| Why is the CRS version pinned? | An image update must never change a policy's behaviour | [0141](adr/0141-waf-detect-first-and-crs-pinning.md), [0144](adr/0144-waf-image-and-agent-contract.md) |
| Why is the JavaScript challenge native HAProxy? | No sidecar, no secret, nothing that can crash separately | [0171](adr/0171-javascript-challenge.md) |
| Why no HS256 for JWT checks? | Engines never hold shared secrets for tokens | [0135](adr/0135-jwt-validation-for-apis.md) |
| Why do secret references have to stay in the tenant's path? | A tenant must not name another tenant's or the platform's secrets | [0071](adr/0071-secret-reference-scope.md), [0072](adr/0072-hardening-engines-logins-secrets.md) |
| Why is a directory login run centrally, one Dex per directory? | Stateless engines, and no client can pick another tenant's directory | [0137](adr/0137-directory-logins-saml-ldap-local.md) |

## Operations and integrations

| Question | Answer | ADR |
| --- | --- | --- |
| Why three Compose nodes with Patroni? | HA without Kubernetes on a two-host Proxmox estate | [0048](adr/0048-production-deployment-shape.md) |
| Why is the management tenant locked? | The controller's own VIP must not be deleted by accident | [0051](adr/0051-management-tenant-lock-and-adopt.md), [0155](adr/0155-management-tenant-at-startup.md) |
| Why does the controller evaluate alerts instead of Alertmanager? | Rules use what only the controller knows, scoped per tenant | [0120](adr/0120-alerting-model.md) |
| Why GSLB on PowerDNS LUA records? | No daemon of ours in the answer path; DNS keeps working without the controller | [0160](adr/0160-gslb-on-powerdns.md) |
| Why is the Kubernetes connector a poller, not an in-cluster controller? | Nothing to install in the cluster; read-only token | [0151](adr/0151-kubernetes-connector.md) |
| Why OpenTofu and OpenBao, not Terraform and Vault? | Avoid BSL-licensed tools | [0146](adr/0146-opentofu-provider-from-the-openapi.md), [CLAUDE.md rules](https://git.example.com/globius-oss/switchyard/-/blob/main/CLAUDE.md) |
| Why MkDocs for this site? | Plain Markdown, static output for GitLab and GitHub Pages | [0190](adr/0190-docs-site-mkdocs-material.md) |

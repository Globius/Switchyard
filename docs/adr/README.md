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
| [0030](0030-rbac-matrix-and-access-lifecycle.md) | Permission matrix, multi-group grants, role refresh, users and token scope | Proposed |
| [0040](0040-api-completeness-for-the-ui.md) | API completeness for the UI: audit, IPAM, health, telemetry, events, documentation rules | Accepted |
| [0041](0041-agent-member-probe-and-withdrawn-prefixes.md) | Agent member probe (health monitor test) and the withdrawn prefix list | Accepted |
| [0018](0018-saml-login.md) | SAML 2.0 login (signxml, signed assertions, replay cache, SP metadata) | Accepted |

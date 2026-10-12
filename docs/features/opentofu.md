# OpenTofu provider

**What.** A provider in `provider/` that manages a tenant's networks, VIP pools, SE groups, services, pools, health monitors,
redirect and allow-list policy sets and routing profiles with OpenTofu. Its schema is generated from the OpenAPI document.

**How to use.** Build it and point it at the controller with a tenant token; see
[provider/README.md](../../provider/README.md). Every write is `?apply=true`, so the canary, revision and Git mirror apply as for
any client.

**Limits.** Certificates are a data source only, so keys never enter OpenTofu state. Tenants that require approval refuse
`apply=true`, so the provider does not fit them yet. Writes are serialised.

**Decision:** [0146](../adr/0146-opentofu-provider-from-the-openapi.md).

# Multi-tenancy

Switchyard serves several teams or customers from one controller and one set of Proxmox hosts. This paper describes what a
tenant owns, how isolation is enforced, and what is shared.

## What a tenant is

A tenant owns its networks, VIP pools, SE groups and engines, virtual services, pools, health monitors, certificates,
policy sets, routing profiles, alert rules, GSLB wide IPs and Kubernetes clusters, plus its change sets, revisions, jobs and
audit trail. People get roles **per tenant** through identity-provider group mappings; automation gets tenant-scoped API
tokens. System administrators manage the platform (identity providers, ACME providers, settings, the `management` tenant)
and can see across tenants.

## Isolation, layer by layer

| Layer | Mechanism |
| --- | --- |
| API | every route is tenant-scoped (`/tenants/{t}/...`); the service layer checks the caller's role for that tenant |
| Database | PostgreSQL row-level security on every tenant table; composite foreign keys keep references inside one tenant; admin work uses a separate role ([ADR 0002](../adr/0002-tenancy-rls.md), [0005](../adr/0005-schema-and-id-conventions.md)) |
| Validation | references to another tenant's objects fail (`wrong_tenant`); secret references must stay in `kv/tenants/<tenant>/` ([0071](../adr/0071-secret-reference-scope.md)) |
| Data plane | SE groups belong to a tenant, so tenants do not share engines by default; per-tenant quotas cap objects, table sizes and cache memory ([0045](../adr/0045-tenant-quota-enforcement.md)) |
| Telemetry | logs and metrics queries are built by the controller with the tenant label, and Grafana is proxied with the same rewriting ([0085](../adr/0085-grafana-behind-controller.md), [0123](../adr/0123-tenant-log-api.md)) |
| Shared names | GSLB zones are assigned by a system administrator and may not nest inside another tenant's zone; Proxmox discovery tags must start with `sy-<tenant>.` ([0145](../adr/0145-pool-discovery-by-proxmox-tag.md), [0162](../adr/0162-gslb-zones-ownership-and-dnssec.md)) |
| Error messages | tenants see fixed sentences for engine failures; real errors stay with system administrators ([0072](../adr/0072-hardening-engines-logins-secrets.md)) |
| Edge logins | each tenant directory runs in its own Dex instance, so one tenant's client cannot use another's directory ([0137](../adr/0137-directory-logins-saml-ldap-local.md)) |

RLS isolation has its own tests against a real PostgreSQL: a query under one tenant's session must never return another
tenant's rows.

## Roles

The permission matrix ([ADR 0030](../adr/0030-rbac-matrix-and-access-lifecycle.md)) defines what each role may do: read,
edit applications, edit infrastructure, apply, approve, roll back, change runtime state, manage tenant settings and access.
`GET /permissions` publishes it, so the UI can hide what a user may not do; the API enforces it regardless. Tenants can
require a second person to approve a change set.

## What is shared

- **Proxmox hosts and SDN.** Engines of different tenants run on the same hosts; placement and capacity are visible per group.
  Networks are tenant objects applied through Proxmox SDN.
- **The controller and its database**, protected by the layers above.
- **OpenBao**, with per-tenant paths and a controller policy that keeps it inside them.
- **Operator-level sources**: ACME provider accounts, geo and reputation feeds, PowerDNS, the telemetry stack.

## The management tenant

The controller's own VIP and engines live in a locked `management` tenant, created at start-up. Changes to it need a system
administrator and a reason, and its `controller` service cannot be deleted or disabled
([ADR 0051](../adr/0051-management-tenant-lock-and-adopt.md), [0155](../adr/0155-management-tenant-at-startup.md)).

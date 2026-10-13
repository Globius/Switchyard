# 0199. Tenant self-service portal: usage, limits and quota requests

Status: Accepted
Date: 2026-10-12

## Context
Phase 3 of the plan (01) and item D8 of the parity plan (08) list a "tenant self-service portal", with
the note "quotas, billing decision". The planned-vs-built audit of 2026-10-12 found nothing built.
Nothing else in the plan says what the portal contains, and there is no design for it.

Most of what a tenant needs to do for itself already exists in the tenant pages: publish applications
(wizard and change sets), certificates, API tokens, role mappings, DNS credentials, Kubernetes
clusters. The one thing a tenant cannot do alone is raise its own quota, because `change quota` is a
system-administrator permission (ADR 0030, 0045). Today that is an e-mail to an administrator.

## Decision
- **The portal is one tenant page, "Usage and requests"** (`/t/:tenant/portal`, under the Tenant nav
  group). It shows each quota with the objects in use (`usage` from `GET /tenants/{t}`), says in words
  and with an icon when a limit is nearly full (80 %) or full, offers "Ask for more", lists the
  tenant's requests with the answer, and links to the pages a new tenant starts from (publish an
  application, people and tokens, certificates). Every role in the tenant can read it.
- **Quota requests are the new mechanism.** A tenant administrator files a request for one quota key,
  a higher limit and a reason (at most 500 characters). Rules, in `switchyard.domain.quota_requests`:
  the tenant must already have a limit for the key (an unlimited resource has nothing to raise), the
  new limit must be higher than the current one and at most 1,000,000, and a tenant has at most one
  pending request per key. The requester can withdraw a pending request.
- **A system administrator decides.** The queue is `GET /quota-requests` (pending by default) and the
  admin page "Quota requests", with a count in the nav. Approving sets the tenant's quota to the
  requested limit, or to a smaller grant between the current and the requested limit, in the same
  transaction as the decision, so the two cannot disagree. Approval never lowers a quota, and it
  fails with `409` when the quota was raised or removed meanwhile. Denying needs a note. Both can
  carry a note the tenant reads.
- **Permissions.** Two matrix entries: `request quota_request` (tenant-admin, in the "tenant settings"
  row) and `decide quota_request` (system-admin, next to `change quota`). Reading follows
  `read tenant_data`. The permission table in 02 is updated with them.
- **Storage.** Table `quota_request` (migration 0040) with the tenant's row-level security policy;
  the decision runs under the admin role because it reads across tenants.
- **Audit.** `quota_request.create`, `.withdraw`, `.approve` and `.deny`, in the tenant's audit log.
- **No billing.** The "billing decision" is: none in the controller. The portal shows usage (showback)
  only. Billing and chargeback are left to whatever consumes the metrics and the audit log.
- **No automatic approval and no notifications** in this change. The admin nav shows a count; e-mail or
  webhook notices go through the alerting targets later if wanted.

## Consequences
New API: `GET`/`POST /tenants/{t}/quota-requests`, `POST .../{request}/withdraw`, `GET /quota-requests`,
`POST /quota-requests/{request}/approve` and `/deny`. The OpenAPI document and the typed UI client
change; the OpenTofu provider does not, because requests are a workflow, not desired state. Only quotas
can be requested. Requests for a new SE group, network or VIP pool, which are infrastructure a tenant
administrator can already create, are not needed, and a request for an SE group on shared Proxmox
capacity stays a conversation with the platform team.

## Alternatives considered
Letting tenant administrators raise quotas up to a platform ceiling: simpler, but it moves the cost
decision to the tenant and needs a ceiling per resource that nobody has defined. Free-form support
tickets in the UI: no structure for the administrator, no audit trail, no state for the tenant.

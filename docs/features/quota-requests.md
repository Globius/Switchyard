# Usage, quotas and quota requests

**What.** A tenant can have limits on how many of each object it owns (`virtual_service`, `pool`, `certificate`, and so on) and
on stick table entries. The **Usage and requests** page (Tenant group in the left nav) is the tenant's portal: each limit with
what is used, a word and an icon when it is nearly full (from 80 %) or full, and a button to ask the platform for more. Only
system administrators change quotas, so a tenant administrator files a *quota request* and a system administrator decides it.

**How to use.**

1. A tenant administrator opens **Usage and requests**, presses **Ask for more** on a limit, enters the new limit and why, and
   sends it. The row shows *Request pending* until it is decided; the request can be withdrawn.
2. A system administrator opens **Admin > Quota requests** (the nav shows how many wait). **Approve** raises the quota at once,
   by default to the limit asked for, or to a smaller number between the current limit and the requested one. **Deny** needs a
   note. Both may carry a note the tenant reads next to the answer.

The same through the API (`/api/v1`):

```bash
# tenant administrator
curl -X POST "$URL/tenants/acme/quota-requests" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"resource": "virtual_service", "requested_limit": 50, "reason": "Two teams join in November"}'
curl "$URL/tenants/acme/quota-requests?state=pending" -H "Authorization: Bearer $TOKEN"
curl -X POST "$URL/tenants/acme/quota-requests/$ID/withdraw" -H "Authorization: Bearer $TOKEN"

# system administrator
curl "$URL/quota-requests" -H "Authorization: Bearer $ADMIN_TOKEN"            # pending, oldest first
curl -X POST "$URL/quota-requests/$ID/approve" -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"granted_limit": 40, "note": "40 for now"}'
curl -X POST "$URL/quota-requests/$ID/deny" -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"note": "Delete unused services first"}'
```

**Limits.**

- The tenant must already have a limit for the resource: a resource without a quota is unlimited, so there is nothing to
  raise. The new limit must be higher than the current one (at most 1,000,000), and a tenant has one pending request per
  resource.
- A grant is between the current limit and the requested one; approving never lowers a quota. If the quota was raised or removed
  while the request waited, approving answers `409` and the request can be denied or left.
- Anyone who can read the tenant can read its requests; only tenant administrators ask or withdraw, and only system
  administrators decide.
- There is no billing. The page shows usage only; charge-back is left to whatever reads the metrics and the audit log. There are no
  e-mail or webhook notices for requests yet.
- Every step is in the audit log as `quota_request.create`, `.withdraw`, `.approve` and `.deny`.

**Decisions:** [0045](../adr/0045-tenant-quota-enforcement.md), [0199](../adr/0199-tenant-portal-and-quota-requests.md).

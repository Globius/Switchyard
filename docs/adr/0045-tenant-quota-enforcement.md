# 0045. Tenant quota enforcement and usage counts

Status: Accepted
Date: 2026-10-11

## Context

Tenant `quotas` were stored and editable but nothing enforced them, and the UI had no usage to show beside the limits
(06 planned-vs-built).

## Decisions

1. Quota keys are object type names (`virtual_service`, `pool`, `network`, ...). `POST /tenants` and
   `PATCH /tenants/{t}` reject other keys and negative limits (`422`).
2. Enforcement is a domain rule in `apply_ops`, so it appears as a problem (`quota_exceeded`, path `quota.<type>`) in the
   draft, the diff and the `422` of `?apply=true`, and it blocks apply like any other problem.
3. Only types that gained objects are checked. A tenant that is over a lowered quota can still edit and delete; it
   just cannot add more of that type.
4. Tenant reads (`GET /tenants`, `GET /tenants/{t}`) return `usage`, the object count per type (zeros included).

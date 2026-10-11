# 0071. Secret references stay inside the tenant

Status: Accepted
Date: 2026-10-11

## Context

Certificates, BGP neighbours and engines name OpenBao paths, and the controller reads them on the tenant's behalf. Nothing stopped a
tenant naming another tenant's path or a platform secret (docs/security/backend-review.md, F1).

## Decisions

1. `check_secret_ref_scope(ref, tenant_name)`: a path under `kv/tenants/` must have the tenant's own name as its next segment, and paths
   under `kv/proxmox/`, `kv/platform/`, `kv/system/` and `kv/controller/` are refused. Other paths stay allowed, because deployments share
   some (for example one Data Plane API credential for all engines).
2. The snapshot validator applies it to `secret_ref`, `crl_secret_ref` and BGP `md5_ref` (code `secret_ref_not_allowed`, blocking apply),
   and engine registration and SE group deploy apply it to `credentials_ref` (422).
3. Imports and issuance already generate `kv/tenants/<tenant>/certs/...` references, so normal use is unchanged.
4. This is a guard, not a policy: the controller's OpenBao policy should also be limited (review finding O3).

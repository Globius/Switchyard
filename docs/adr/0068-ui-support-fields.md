# 0068. Small fields the UI asked for

Status: Accepted
Date: 2026-10-11

## Context

Four screens lacked one value each (06 planned-vs-built, 07 UI vs artboards).

## Decisions

1. **Permission groups.** Each entry of `GET /permissions` has `group`, one of eight names derived from the rows of the matrix table in
   02: `Read`, `Applications`, `Infrastructure` (rows 3 and 13), `Change sets` (rows 4 to 9), `Runtime` (10 to 12), `Tenant settings`
   (14, 15), `Access` (16 to 19) and `System`. The table is not changed; the mapping lives beside it in `domain/permissions.py`, and a test fails
   when a new row has no group.
2. **Grafana link.** `SWITCHYARD_GRAFANA_URL` (http or https only, no credentials in the URL) is returned as `links.grafana` by `GET /me`, `null`
   when unset. The controller only passes the URL on; it does not proxy Grafana or check it.
3. **Member count.** Tenant reads (`GET /tenants`, `GET /tenants/{t}`) have `members`: the people whose stored provider groups match a role
   mapping of the tenant, the same rule as `GET /tenants/{t}/users`. Disabled accounts count.
4. **SDN apply state.** `GET /tenants/{t}/sdn/status` lists the managed networks with `state` `queued`, `running`, `succeeded`, `failed` or
   `never`, and the job it comes from. An apply is tenant-wide, so a queued or running job counts for every managed network; after it,
   the latest finished job decides, and a network that job did not report is `never`.

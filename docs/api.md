# HTTP API

The contract is `docs/api/openapi.json`; the running controller serves the same document at
`/api/v1/openapi.json`, an interactive Swagger UI at `/api/v1/docs` and ReDoc at `/api/v1/redoc`.
Regenerate the file with `UPDATE_OPENAPI=1 uv run pytest tests/unit/test_openapi.py` from `controller/`,
review the diff and commit it; CI rejects breaking changes. Background: [ADR 0014](adr/0014-typed-api-contract.md),
[ADR 0040](adr/0040-api-completeness-for-the-ui.md).

Every operation has a summary, a description, typed request and response models, an example, the error
responses it can produce and its **access** rule (`x-required-role`: `public`, `authenticated`, `viewer`,
`operator`, `tenant-admin` or `system-admin`). `tests/unit/test_api_docs.py` fails when one of them is missing.

## Authentication

- **Bearer token** (`Authorization: Bearer sy_...`): for machines. Create with `POST /tenants/{t}/tokens`.
- **Session cookie** (`sy_session`) from the login flow (OIDC, SAML, break-glass): for the browser. Requests that
  change something must also send `X-CSRF-Token` with the `csrf_token` from `GET /me`.
  `GET /me` also returns `principal`, the author string stored on your drafts (`user:<provider>:<subject>`).
- Public: `GET /healthz`, `GET /auth/providers`, the login redirects and callbacks, SAML metadata,
  `POST /auth/break-glass`, `POST /enroll` (the enrollment token is the credential).

Roles nest: `viewer` < `operator` < `tenant-admin` < `system-admin`. Operators edit services, pools, monitors,
certificates and policies and apply change sets; tenant administrators also edit networks, VIP pools, SE groups,
routing profiles and engines, approve, roll back and manage tokens; system administrators manage tenants and see
the cross-tenant audit log.

## Responses and errors

- Every 2xx JSON body has a model and an example. Free-form on purpose: an operation's `body`, a job's `result`
  for kinds other than apply, an audit event's `diff`.
- Errors are `application/problem+json`: `type`, `title`, `status`, `detail`, optional `errors[]` (`path`, `code`,
  `message`; `path` addresses the input field, for example `ports.0.port`) and, on a stale version, `conflict`.

| Status | Meaning |
| --- | --- |
| 400 | Malformed request, for example an invalid `cursor` |
| 401 | Missing, invalid or expired credentials |
| 403 | The role does not allow it (also: wrong CSRF header; tenant requires approval) |
| 404 | Unknown tenant or object, or not visible to the caller |
| 409 | State conflict (a change set that is not a draft, a taken name, a pool with no free address) |
| 412 | `If-Match` or an operation's `expected_version` is stale |
| 422 | The request or the resulting state breaks a rule; read `errors[]` |
| 428 | `If-Match` is required |

- Object reads return `ETag: "<version>"`. Send it as `If-Match` on `PATCH` and `DELETE`.
- **Stale operations (`412`).** The problem carries `conflict: {object_type, object_id, expected_version,
  current_version}`. A three-way view is: *base* = what the client had read, *theirs* = `GET` of the object,
  *yours* = the operation `body`; resend with `expected_version: current_version`.
- Timestamps are ISO 8601 UTC.

## Objects

Reads of an object type (`GET /tenants/{t}/pools`, …) return the object's fields plus `draft_status`
(`unchanged | created | modified | deleted`) and `changed_fields`; with `?change_set=<id>` they show the draft.
Lists are `{items, next_cursor}` ordered by name.

List filters (all lists): `name` (exact), `q` (substring), `sort=name|-name`, `change_set`, `cursor`, `limit`
(1–200). Extra filters: virtual services `se_group`, `protocol`, `enabled`, `pool`, `certificate`; pools
`health_monitor`; policy sets and routing profiles `kind`; SE groups `ha_mode`; certificates `source`,
`expires_within_days`.

Policy sets and routing profiles are unions on `kind`:

| Collection | `kind` values | Fields |
| --- | --- | --- |
| `policy-sets` | `redirect` | `rules[]` (`match`, `redirect`) |
| | `allow_list` | `cidrs[]`, `managed_by` |
| | `rewrite` | `rules[]` (`phase`, `match`, `action`: `set_header`, `add_header`, `remove_header`, `replace_prefix`), ADR 0102 |
| | `respond` | `rules[]` (`match`, `status`, `content_type`, `body`, `retry_after_s`), ADR 0103 |
| | `error_pages` | `pages[]` (`status`, `content_type`, `body`), ADR 0103 |
| `routing-profiles` | `vrrp` | `vrid`, `interface`, `advert_interval_s`, `base_priority`, `priority_step`, `static_routes[]` |
| | `bgp` | `asn`, `bfd`, `peers[]` (`peer_ip`, `remote_asn`, `md5_ref`, `max_prefixes`), `max_prefixes`, `max_paths`, `static_routes[]` |

`kind` is required when creating and when patching (and cannot be changed).

## Pagination

Lists of objects, change sets, revisions, jobs, audit events and access logs take `limit` and `cursor` and return
`{items, next_cursor}` (`entries` for logs); pass `next_cursor` back to continue. Cursors are opaque.

## Change sets

`GET /tenants/{t}/change-sets/{cs}/diff` returns `objects`, `problems` (rules the new state breaks; they block
apply), `warnings` (`{code, path, message}`; they never block), `impact` (services, VIPs, SE groups and hostnames
the change reaches) and `se_groups` with the changed files (`before`, `after`, unified `diff`).

`POST …/apply` takes an optional `message` (up to 2000 characters) that is kept as the revision message: revisions return `message`, and the Git mirror commit body carries it.

Tenant quotas (`quotas` on `POST /tenants` and `PATCH /tenants/{t}`) are keyed by object type name, for example `{"virtual_service": 50}`. A change that adds objects past a limit fails with problem `quota_exceeded` (path `quota.<type>`); deleting and editing stay possible when a tenant is over a lowered quota. `GET /tenants` and `GET /tenants/{t}` return `usage`, the object count per type, and `members`, the number of people with a
grant in the tenant.

`POST …/apply` returns `202 {job_id}`. When the tenant has `require_approval` it answers `403` unless the caller is
a system administrator who sends `break_glass_reason`; use `request-approval` then. The approver must be a
tenant administrator and not the author or requester.

## Status, health and live events

- `GET /tenants/{t}/status/engines`: per engine `reachable`, `members` (`backend`, `server`, `status`), `agent`
  (`bgp[]`, `vrrp[]`, `ipvs[]`, `routes_withdrawn`, `withdrawn_prefixes[]`) and `drift[]`. Drift is reported, never corrected.
- `GET /tenants/{t}/status/virtual-services`: `state` (`serving`, `disabled`, `not_served`) and `health`
  (`healthy`, `degraded`, `down`, `unknown`, `disabled`) with `members_up` / `members_total`. Rule: look at the
  reported member rows of the service's pools on the reachable engines of its SE group; `healthy` if every row is `UP`
  and every reachable engine reported, `down` if none is `UP` or a pool has no members, `unknown` if nothing was
  reported, otherwise `degraded`. Draining is not `UP`.
  `members[]` lists the rows behind the counts, one per engine and pool member (`pool`, `member_id`, `address`, `port`,
  `engine`, `status`, `check_status`, `last_check`, `last_change_s`, `last_checked_at`); the same rows come in the
  `vs_state` event. `failing_members` of the summary carry the check fields too. `last_checked_at` is when the
  controller read the engine, see ADR 0047.
- `GET /tenants/{t}/summary`: the tenant home in one call (services by health, failing members, drifted and
  unreachable engines, certificates expiring within 30 days, job counts, open drafts, latest revision).
- `POST /tenants/{t}/se-groups/{group}/reapply` (`tenant-admin`, 202 with a job) is the one explicit drift repair: it pushes the current
desired state to the group's engines again, canary first, without a revision (job `reapply_se_group`, ADR 0069).
- `GET /tenants/{t}/status/stream` (server-sent events): `status` (array), `vs_state` (array), `job_progress` (one
  job), `revision_applied` (one revision summary). Every event has an `id`; a reconnect restarts from a snapshot.
  Payload models are under `x-sse-events` of the operation.

## Metrics and logs

`GET /tenants/{t}/virtual-services/{id}/metrics?window_s&step_s`, `GET /tenants/{t}/metrics/virtual-services?window_s`
and `GET /tenants/{t}/virtual-services/{id}/logs?since&until&q&status_class&cursor&limit` read Prometheus and Loki
(see [operating](operating.md)). With no backend configured, or when it fails, they answer `200` with
`available: false` and a `reason`; a metric with no data is `null`, not 0.

## Health monitor test

`POST /tenants/{t}/health-monitors/{id}/test` runs the monitor once from an engine (through its agent) against one pool
member and returns `ok`, `latency_ms`, `detail`, `engine`, `target`; nothing is saved. The agent only probes members in its
applied configuration (`409` otherwise; `502` if the agent is unreachable). ADR 0041.

## VIP pools

`GET …/vip-pools/{id}/allocations` returns `total`, `used`, `free`, the holding services (address, service,
`draft_status`) and `next_free`. `POST …/vip-pools/{id}/next-free` suggests the lowest free address without
reserving it. An address is held by the service that carries it; releasing it means changing or deleting that
service in a change set.

## Engines

Engines also return `host_online`: whether Proxmox reports their node online (`null` without a node or without Proxmox).
`GET /tenants/{t}/se-groups/{group}/placement` returns the cluster nodes (`online`, `cpu`, `mem_used`/`mem_total`, `storage[]`) and the
group's `engines[]` on each, for the Placement card; `available: false` when Proxmox is not configured or not reachable
(set `SWITCHYARD_PVE_URL`, `SWITCHYARD_PVE_TOKEN_ID` and the token secret for `syctl serve` as for the worker; the token needs
`Sys.Audit` and `Datastore.Audit`). Answers are cached for 15 seconds. See ADR 0049.

`GET /tenants/{t}/engines` (filters `se_group`, `state`) and `GET …/engines/{id}` return placement (`pve_node`,
`vmid`) and agent enrollment. **Retire** (`POST …/retire`, a job) destroys the VM and removes the record. **Remove**
(`DELETE`) deletes the record only. `deploy` clones VMs and registers engines but does not create enrollment tokens;
create one per engine (`POST …/enrollment-token`). `nic_pools` on deploy maps each VIP or backend network id to the address pool its NIC addresses come from (ADR 0057; omit it to leave those NICs unaddressed). `guest_kind` on deploy is `qemu` (default) or `lxc` (container template; no cloud-init, ADR 0055). `ssh_public_keys` on deploy is for human access to the VM
(cloud-init); the controller never uses SSH.

## Certificate import

`POST /tenants/{t}/certificates/import` takes `{name, pem}` (chain then unencrypted key) **or**
`{name, pkcs12_b64, password}` (a base64 PKCS#12/PFX; `password` may be omitted when the file has none).
Exactly one source is accepted. The controller unpacks leaf, chain and key, checks the key matches the leaf and
stores the same PEM bundle in OpenBao as for a PEM import; the password is never logged, audited or stored.
Errors are 422 problem+json with `errors[].path` `password` (wrong password), `pkcs12_b64` (not base64, not a
PKCS#12, no key or certificate, legacy RC2/3DES encryption: re-export with AES). See ADR 0043.

`POST /tenants/{t}/certificates/{id}/replace` takes the same `pem` or `pkcs12_b64` + `password` (no `name`) and
updates the existing certificate object in a change set (`?apply=true` or `?change_set=`). The object keeps its id and
name, so every virtual service using it is unchanged; the secret reference, names and expiry change, `source` becomes
`upload` and `renew_policy` `manual`. Unknown id: 404. See ADR 0044.

`POST /tenants/{t}/certificates/import-ca` (`{name, pem, crl_pem?}`) imports a client CA (certificates only, optional CRL) as a
certificate with `purpose: client_ca`. A TLS port names it as `client_ca_id` with `client_verify` `required` or `optional`; the
validator reports `wrong_certificate_purpose` for mix-ups. See ADR 0070.

Certificates carry `renew_before_days` (1 to 365, default 30): an `auto` certificate is renewed that many days before it
expires. Read it with the certificate, change it with a normal update operation. See ADR 0064.

## Jobs

`kind` is an enum: `apply_change_set`, `deploy_se_group`, `retire_engine`, `apply_sdn`, `issue_cert` (result
`{change_set_id}`: the `system:certs` change set that records the certificate), `renew_certs`,
`sync_cloudflare_ips`, `poll_status`, `git_mirror`, `gc`. States: `queued`, `running`, `succeeded`, `failed`, `dead`,
`cancelled`.

`GET /permissions` gives every entry a `group` (eight names) for the grouped role matrix; `GET /me` returns
`links.grafana` (from `SWITCHYARD_GRAFANA_URL`, else `null`). `GET /tenants/{t}/sdn/status` lists each managed network's SDN apply
state (`queued`, `running`, `succeeded`, `failed`, `never`). See ADR 0068.

## Engine views

- `GET /tenants/{t}/virtual-services/{service}/engines`: the enabled engines of the service's SE group.
- `GET /tenants/{t}/virtual-services/{service}/engines/{engine}/rendered`: `files[]` (`file`, `scope` `group|engine`, `language`, `content`) and the
  render `fingerprint`; no certificates or keys, BGP passwords as `REDACTED`.
- `GET /tenants/{t}/engines/{engine}/running-config`: asks the engine now. `haproxy` has the running `content`, its `fingerprint`, the
  `desired_fingerprint`, `fingerprint_matches` and `matches_desired` (stamp and text); `agent` lists each routing file's `sha256` against
  `desired_sha256` (hashes only: the agent cannot read files back). Tenants get a fixed `error` sentence. See ADR 0073.

## System settings

`GET /system/settings` and `PATCH /system/settings` (`system-admin`) read and change controller-wide settings; today `allow_lxc_guests` (default
`false`) with `allow_lxc_guests_forced` when `SWITCHYARD_ALLOW_LXC=1` forces it on. While it is off, a deploy with `guest_kind: lxc` answers `403`
with code `lxc_disabled`. `GET /me` returns `features.lxc_guests` for the UI. See ADR 0076.

## Controller metrics and discovery

`GET /metrics` (outside `/api/v1`) and `GET /api/v1/discovery/prometheus?exporter=haproxy|node&port=` are for Prometheus: the
controller's own metrics and `http_sd_configs` targets for the SEs, labelled `tenant`, `tenant_name`, `se_group`, `engine`, `vs`.
System administrators or the `SWITCHYARD_METRICS_TOKEN` bearer token only. See `docs/operating.md` and ADR 0061.

## Audit

`GET /tenants/{t}/audit-events` and, for system administrators, `GET /audit-events?tenant=` filter by `actor`,
`action` (a trailing `*` means prefix), `object`, `since`, `until` and `q`, newest first, with a cursor.
`GET /audit-actions` lists every action name with a category and description.
`GET /tenants/{t}/audit-events/export` and `GET /audit-events/export` take the same filters and stream every matching event
as a file (`format=csv|jsonl`, no paging, default `csv`). Each export is audited as `audit.export`. See ADR 0060.

`GET /tenants/{t}/principals?actor=user:…&actor=token:…` (and `GET /principals` for system administrators) turns the
labels audit events, change sets and revisions carry into `display_name`s (`kind`: `user`, `token`, `local`, `login`,
`system`, `unknown`). A user only resolves to a provider name when they appear in the tenant's own history. See ADR 0062.

## Search

`GET /tenants/{t}/search?q=&types=&limit=` finds objects by name; virtual services also match their VIP, certificates
their SANs and pools their member addresses.

## Schema helper

`GET /schema/virtual-service?protocol=http|tcp|udp` returns the fields the protocol rejects on the service
(`service_forbidden`), on its ports (`port_forbidden`) and on its pools (`pool_forbidden`), the allowed pool
algorithms and monitor types, and whether pool members need a port, so a form can disable fields. The validator
reads the same table.

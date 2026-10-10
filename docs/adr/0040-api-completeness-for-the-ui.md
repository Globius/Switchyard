# 0040. API completeness for the UI: what was added and the rules behind it

Status: Accepted
Date: 2026-10-10

## Context

The gap audit in `docs/design/05-frontend-backend-tickets.md` (A1 to A12) found the API complete for
CRUD, change sets, jobs, certificates and deployment, but missing what the screens read around them:
audit filters, IP usage, search, a tenant summary, engine placement, service health, metrics and
logs, live events and conflict data. Claude Design also asked for decisions (what *release* means, what
*retire* means, what a refused apply looks like). This record fixes them.

## Decisions

1. **Audit log.** `GET /tenants/{t}/audit-events` is a `Page` (it was a bare array) with filters
   `actor`, `action`, `object` (`*` suffix means prefix), `since`, `until`, `q`, `cursor`, `limit`.
   The cursor is `<microseconds since epoch>|<event id>` over `(at desc, id desc)`, which is stable under
   inserts. `GET /audit-events` (system-admin) spans tenants and includes events that belong to none;
   `GET /audit-actions` is the catalog of action names with category and description. The catalog is
   code (`switchyard/db/audit_actions.py`) and a test fails when a new `action=` literal is missing, so
   labels cannot drift. History of one object is `?object=<type>:<id>` on the audit log and
   `?object=<id>` on revisions (revisions whose change set had an operation on it).
2. **VIP allocation has no reservation.** An address is held by the virtual service that carries it as
   `vip`. `GET …/vip-pools/{id}/allocations` derives used/free/holders (with `draft_status` under
   `?change_set=`) and `POST …/next-free` suggests the lowest free address without reserving it. *Release*
   means changing or deleting the service in a change set; there is no release call. A second service
   that picks the same address is stopped by the existing validator, so races are safe.
3. **Virtual service health** is derived, not stored (`GET …/status/virtual-services`, field `health`):
   `disabled`, `unknown` (no reachable engine or nothing reported), `healthy` (every reported member row
   `UP` and every reachable engine of the group reported), `down` (no row `UP`, or a pool without members),
   `degraded` (otherwise). Draining counts as not `UP`. `members_up` and `members_total` give the ratio.
4. **Engines.** `EngineRead` carries `se_group_id`, `mgmt_ip`, `agent_url`, `pve_node`, `vmid`,
   `agent_enrolled`, `agent_cert_serial`; `GET …/engines` and `GET …/engines/{id}` exist. Host online state
   is not exposed (the Proxmox adapter has no node-status call yet). **Retire** destroys the Proxmox VM and
   removes the record (a job; the last serving engine is refused). **Remove** (`DELETE`) deletes the
   record only and leaves any VM running, for hand-built or vanished engines. `ssh_public_keys` on deploy
   goes into cloud-init for *human* access to the VM; the controller never connects over SSH (CLAUDE.md).
   `deploy_se_group` does **not** create enrollment tokens: secrets do not belong in cloud-init, so a
   token is created per engine (ADR 0011) and the UI shows the `sy-agent enroll` command.
5. **Apply under approval.** `POST …/apply` answers `403` when the tenant has `require_approval` and the
   caller is not a system administrator sending `break_glass_reason`; the UI hides Apply from
   `tenant.require_approval` and the caller's role, and offers *Request approval*. A machine-readable
   `code` on that problem arrives with the permission work (G1-1); until then the `detail` is stable.
6. **Conflicts.** A stale `expected_version` answers `412` with a `conflict` member
   (`object_type`, `object_id`, `expected_version`, `current_version`). The three-way view needs no server
   data: *base* is what the client read, *theirs* is `GET` of the object (its `ETag` is
   `current_version`), *yours* is the operation `body`. The diff gains `impact`: services, VIPs, SE groups and
   hostnames the change reaches, including through pools, monitors, certificates, policy sets, networks,
   VIP pools and routing profiles.
7. **Live events.** `GET …/status/stream` carries `status`, `vs_state`, `job_progress` and
   `revision_applied` with ids; a reconnect starts again from a snapshot (no `Last-Event-ID`). Payload
   models are listed under `x-sse-events` in OpenAPI. The server still polls the database; pushing from
   the worker is a later optimisation behind the same contract.
8. **Metrics and logs** come from Prometheus and Loki through `adapters/telemetry.py`. Contract with the
   telemetry pipeline: Prometheus series carry `tenant` and HAProxy's `proxy="vs_<service>"`; Loki holds the
   SE's `httplog` lines with labels `tenant` and `frontend="vs_<service>"`. Every query puts the tenant
   matcher first and validates the service name, so a caller cannot widen it. Without configuration
   (`SWITCHYARD_PROMETHEUS_URL`, `SWITCHYARD_LOKI_URL`) or on failure the routes answer
   `available: false` with a `reason` (HTTP 200), so screens render a "no data source" state instead of
   an error. The shipping side (exporter scrape config, log shipper on the SE image) is not built yet.
9. **Lists** accept `q`, `sort=name|-name` and per-type filters (`se_group`, `protocol`, `enabled`,
   `pool`, `certificate`, `kind`, `ha_mode`, `source`, `expires_within_days`, `health_monitor`). `?label=`
   is dropped: objects have no labels and none are planned.
10. **Documentation is enforced.** `switchyard/api/docs_meta.py` adds tags, security schemes
    (`bearerAuth`, `sessionCookie` + `csrfToken`), the minimum role of every operation (also as
    `x-required-role`), parameter descriptions, request examples, 401/403/404/409/400 responses and
    pagination notes. `tests/unit/test_api_docs.py` fails when an operation lacks a summary, description,
    access rule, parameter description, success example, request example or problem response. Swagger UI
    is at `/api/v1/docs`, ReDoc at `/api/v1/redoc`, the document at `/api/v1/openapi.json`, and
    `docs/api/openapi.json` is checked against it by `tests/unit/test_openapi.py`. The access table in
    `docs_meta.py` is a documentation table; enforcement stays in the services until the permission
    matrix becomes code (G1-1), which will replace the table.

## Consequences

- `audit-events` changed shape (array to `Page`); the generated UI client is regenerated in the same merge.
- The access table can drift from the services until G1-1; the integration tests that call routes with
  tokens of each role (G1-1) are what will close that.
- Metrics and logs are inert until the telemetry pipeline exists; the contract is fixed so it can be
  built without API changes.

## Alternatives considered

- A reservation table for VIPs: more state and a release endpoint for no gain, since the validator
  already prevents duplicates.
- Storing health in the database: stale between polls and a second source of truth; deriving it from the
  stored engine status is cheap and consistent with `status/engines`.
- Server-side three-way merge data: needs per-version object history that is not stored; the client
  already has the base.

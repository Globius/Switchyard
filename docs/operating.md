# Running and operating the controller

## Processes

| Command | What it does |
| --- | --- |
| `syctl db upgrade` | Apply database migrations |
| `syctl serve` | HTTP API (`/api/v1`; Swagger UI `/api/v1/docs`, ReDoc `/api/v1/redoc`, OpenAPI `/api/v1/openapi.json`) |
| `syctl worker` | Job worker and scheduler (apply, Git mirror, status polling, certificates, housekeeping) |
| `syctl token` | Create a system-admin API token directly in the database (bootstrap and recovery) |

Run any number of workers; one of them (whoever holds the advisory lock) schedules the recurring jobs.

## Configuration (environment)

| Variable | Purpose |
| --- | --- |
| `SWITCHYARD_DATABASE_URL` | PostgreSQL URL |
| `SWITCHYARD_PUBLIC_URL` | External URL; builds login redirect URIs and makes cookies `Secure` |
| `SWITCHYARD_OPENBAO_ADDR`, `_ROLE_ID`, `_SECRET_ID`, `_CA` | OpenBao KV v2 with AppRole; without it secrets come from `SWITCHYARD_SECRET_*` variables (read-only, so no certificate storage) |
| `SWITCHYARD_DATAPLANE_CA`, `_CERT`, `_KEY` | mTLS from the controller to Data Plane APIs and agents |
| `SWITCHYARD_BUNDLE_KEY_FILE` | Ed25519 key (PKCS#8 PEM) that signs agent bundles |
| `SWITCHYARD_PKI_MOUNT`, `SWITCHYARD_PKI_ROLE` | OpenBao PKI role that signs agent certificates (defaults `pki`, `agents`) |
| `SWITCHYARD_PVE_URL`, `_PVE_TOKEN_ID`, `_PVE_CA` | Proxmox API (token secret at reference `kv/proxmox/token`) |
| `SWITCHYARD_CLOUDFLARE_TOKEN` | DNS-01, Origin CA and IP-range sync |
| `SWITCHYARD_ACME_DIRECTORY`, `_EMAIL`, `_ACCOUNT_KEY_FILE` | ACME issuance |
| `SWITCHYARD_PROMETHEUS_URL`, `SWITCHYARD_LOKI_URL` | Metrics (and with Loki: access logs and p95) for the UI; see below |
| `SWITCHYARD_GITLAB_URL`, `_PROJECT`, `_TOKEN` | Git mirror of every revision |
| `SWITCHYARD_SANDBOX=docker` | Validate configuration with `haproxy -c` in a container before pushing |
| `SWITCHYARD_BREAK_GLASS=1` | Allow the local break-glass administrator to log in |

## Logging in people

```bash
syctl idp add authentik --issuer https://sso.example.test/application/o/switchyard/ --client-id switchyard --client-secret-ref kv/idp/authentik
syctl idp map authentik switchyard-admins system-admin
syctl idp map authentik acme-admins tenant-admin --tenant acme
syctl idp list
```

Roles are decided at login from the group claim, so a changed mapping applies at the next login
(sessions last eight hours, one hour idle). The break-glass administrator is created with
`syctl local-admin` and stays unusable until `SWITCHYARD_BREAK_GLASS=1`. See ADR 0012.

## Using the API from the command line

```bash
syctl api login --url https://switchyard.example.test --token sy_...
syctl api whoami
syctl api list pools -t acme
syctl api apply pools -f pool.json -t acme   # one change set, waits for the rollout
syctl api status -t acme                     # reachable engines and drift
```

The token is saved in `~/.config/syctl/config.json` (or `%APPDATA%\syctl\config.json`), readable by you
only. `SYCTL_URL` and `SYCTL_TOKEN` override the file.

## Engines and agents

1. `POST /tenants/{t}/se-groups/{id}/deploy` clones the SE template on Proxmox and registers the engines.
2. `POST /tenants/{t}/engines/{id}/enrollment-token` gives a one-time token; on the VM run
   `sy-agent enroll --controller https://... --token-file /run/switchyard/enroll.token`, and schedule
   `sy-agent renew --controller https://...` (for example daily).
3. `POST /tenants/{t}/engines/{id}/retire` disables an engine, destroys its VM and removes it. The last
   engine of a group that serves traffic is refused. `DELETE /tenants/{t}/engines/{id}` removes only the record
   and leaves any VM running (for hand-built or vanished engines). Deploy does not create enrollment tokens; do
   step 2 for each engine. `ssh_public_keys` on deploy is for people logging in to the VM; the controller never
   uses SSH.

Drift (an engine running something other than the desired state) is reported in
`/tenants/{t}/status/engines` and as `drift.detected` audit events. It is never corrected automatically.

## Jobs

`GET /tenants/{t}/jobs?state=&kind=&cursor=&limit=` lists a tenant's jobs, newest first. `GET /tenants/{t}/jobs/{id}`
shows one job with its `stages`; for an apply they are `lock, render, validate, canary, rollout, commit`, each
with `state` (`pending`, `running`, `succeeded`, `failed`, `skipped`), timing and the error of the stage that
failed. `GET …/jobs/{id}/log` returns the log lines.

- An apply that was rejected or reverted is a **finished** job (`state: succeeded`, `ok: false`); read the
  failing stage and `result.error`. `retrying: true` means an attempt failed and the job waits for its next try.
- `POST …/jobs/{id}/retry` (operator) queues a `dead` job again with a fresh attempt budget.
- `POST …/jobs/{id}/cancel` (operator) cancels a job that has not started; a running job cannot be cancelled.
  Cancelling an apply sets its change set to `failed`, so it can be applied again.

Both are audited. Progress is read by polling. See ADR 0016.

## Metrics and logs

The UI's charts and log tail read Prometheus and Loki through the controller, which adds the tenant to every
query. The telemetry pipeline must provide: Prometheus series from each SE's HAProxy exporter with a `tenant`
label (tenant id) and HAProxy's `proxy="vs_<service name>"`; Loki lines from the SE's HAProxy `httplog` with the
labels `tenant` and `frontend="vs_<service name>"`. Set `SWITCHYARD_PROMETHEUS_URL` (and `SWITCHYARD_LOKI_URL`).
Without them the metrics and log routes answer `available: false` and the UI shows "no data source". The
exporter scrape config and the log shipper on the SE image are not part of the controller yet (ADR 0040).

## Audit log

`GET /tenants/{t}/audit-events` filters by actor, action (`change_set.*` style prefixes), object, time and text;
`GET /audit-events` (system administrators) spans tenants; `GET /audit-actions` lists every action name. A new
`action=` string in the code must be added to `switchyard/db/audit_actions.py` (a test enforces it).

## Housekeeping

An hourly job expires drafts untouched for 14 days, finished jobs after 30 days, stale engine status and
expired login or enrollment material. It never touches desired state, revisions or the audit log.

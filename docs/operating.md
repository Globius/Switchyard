# Running and operating the controller

## Processes

| Command | What it does |
| --- | --- |
| `syctl db upgrade` | Apply database migrations |
| `syctl serve` | HTTP API (`/api/v1`; Swagger UI `/api/v1/docs`, ReDoc `/api/v1/redoc`, OpenAPI `/api/v1/openapi.json`) |
| `syctl worker` | Job worker and scheduler (apply, Git mirror, status polling, certificates, housekeeping) |
| `syctl adopt FILE` | Adopt infrastructure built outside the controller (the `management` tenant bootstrap, its engines and the controller VMs; ADR 0051) |
| `syctl token` | Create a system-admin API token directly in the database (bootstrap and recovery) |

Run any number of workers; one of them (whoever holds the advisory lock) schedules the recurring jobs.

## Configuration (environment)

| Variable | Purpose |
| --- | --- |
| `SWITCHYARD_DATABASE_URL` | PostgreSQL URL |
| `SWITCHYARD_PUBLIC_URL` | External URL; builds login redirect URIs and makes cookies `Secure` |
| `SWITCHYARD_OPENBAO_ADDR`, `_ROLE_ID`, `_SECRET_ID`, `_CA` | OpenBao KV v2 with AppRole; without it secrets come from `SWITCHYARD_SECRET_*` variables (read-only, so no certificate storage) |
| `SWITCHYARD_DATAPLANE_CA`, `_CERT`, `_KEY` | mTLS from the controller to Data Plane APIs and agents. With a bundle signing key the CA is required: the worker and API refuse to start without it unless `SWITCHYARD_INSECURE_AGENT=1` (lab only) |
| `SWITCHYARD_BUNDLE_KEY_FILE` | Ed25519 key (PKCS#8 PEM) that signs agent bundles |
| `SWITCHYARD_PKI_MOUNT`, `SWITCHYARD_PKI_ROLE` | OpenBao PKI role that signs agent certificates (defaults `pki`, `agents`) |
| `SWITCHYARD_GEO_URL`, `SWITCHYARD_REPUTATION_FEEDS` | Sources of country and reputation lists for `managed_by: geo` and `reputation` ([guide](ip-lists.md)) |
| `SWITCHYARD_CROWDSEC_URL`, `_CROWDSEC_API_KEY`, `_CROWDSEC_ORIGINS`, `_CROWDSEC_LOGS_ADDRESS` | CrowdSec local API, bouncer key, decision origins and the UDP address for access logs; the fallback while Admin > Integrations holds nothing ([guide](crowdsec.md)) |
| `SWITCHYARD_PVE_URL`, `_PVE_TOKEN_ID`, `_PVE_CA` | Proxmox API (token secret at reference `kv/proxmox/token`); also turns on pool discovery by tag ([guide](pool-discovery.md)) |
| `SWITCHYARD_CLOUDFLARE_TOKEN` | DNS-01, Origin CA and IP-range sync |
| `SWITCHYARD_ACME_DIRECTORY`, `_EMAIL`, `_ACCOUNT_KEY_FILE` | The ACME provider of earlier versions; on first use it becomes the provider `default`. Manage providers in the UI instead (`docs/acme.md`) |
| `SWITCHYARD_ACME_DNS_WAIT` | Seconds to wait after publishing DNS-01 records (default 15) |
| `SWITCHYARD_CONTROLLER_CN` | Extra names no engine certificate may carry, comma-separated: the common names agents accept from the controller (`sy-agent -controller-cn`). `switchyard-controller` is always reserved (ADR 0210) |
| `SWITCHYARD_K8S_CIDRS` | Private networks that Kubernetes API servers named in cluster registrations may live in (comma-separated); loopback and link-local are never allowed |
| `SWITCHYARD_JWKS_CIDRS` | Private networks that a `jwks_url` of a JWT rule may point into (comma-separated); loopback and link-local are never allowed |
| `SWITCHYARD_DNS_CIDRS` | Private networks that RFC 2136 and PowerDNS servers named in DNS credentials may live in (comma-separated); loopback and link-local are never allowed |
| `SWITCHYARD_METRICS_TOKEN` | Bearer token that may read `/metrics` and the Prometheus service-discovery list (and nothing else); system administrators can too. At least 16 characters; `change-me` and other placeholders stop startup |
| `SWITCHYARD_ENGINE_CIDRS`, `SWITCHYARD_ENGINE_POLICY` | Networks hand-registered engines may live in besides their group's `mgmt` networks (comma-separated); `open` turns the engine address policy off (labs). ADR 0072 |
| `SWITCHYARD_SHARED_SECRET_PREFIXES`, `SWITCHYARD_SECRET_POLICY` | Secret paths every tenant may name besides `kv/tenants/<tenant>/` (each ending in `/`); `open` allows any path (labs). `syctl openbao-policy` prints the matching OpenBao policy. ADR 0072 |
| `SWITCHYARD_TRUST_FORWARDED` | `1` behind a reverse proxy: login throttling uses the last `X-Forwarded-For` entry as the source address |
| `SWITCHYARD_ALLOW_LXC` | `1` forces container (LXC) guests on (labs and the quickstart); otherwise a system administrator enables them with `PATCH /system/settings` or `syctl setting allow_lxc_guests true`. ADR 0076 |
| `SWITCHYARD_AUDIT_SYSLOG` | Also send every audit event to syslog as one JSON line (`udp://host[:514]`, `tcp://host[:514]` or `unix:///dev/log`, facility local0) |
| `SWITCHYARD_POWERDNS_URL`, `SWITCHYARD_POWERDNS_API_KEY`, `SWITCHYARD_POWERDNS_SERVER_ID` (`localhost`) | PowerDNS Authoritative for GSLB (ADR 0160, [gslb.md](gslb.md)); both URL and key turn it on. The server needs `enable-lua-records=yes` |
| `SWITCHYARD_SMTP_HOST`, `_PORT` (587), `_USER`, `_PASSWORD`, `_FROM`, `_STARTTLS` (`0` turns it off) | Mail server for email alert targets (ADR 0121); without a host email targets cannot send |
| `SWITCHYARD_ALERT_ALLOW_HTTP`, `SWITCHYARD_ALERT_ALLOW_PRIVATE` | `1` lets alert webhooks use http and name private addresses (labs). Default: https and public destinations only |
| `SWITCHYARD_GRAFANA_URL` | Grafana base URL (http or https); the UI links to it from `GET /me` `links.grafana` |
| `SWITCHYARD_GRAFANA_INTERNAL_URL`, `_ADMIN_PASSWORD`, `_ADMIN_USER`, `_DS_SECRET`, `_DS_URL` | Grafana embedded in the UI behind the controller (ADR 0085, [runbooks/telemetry.md](runbooks/telemetry.md)). All of the first, second, fourth and fifth plus `SWITCHYARD_PROMETHEUS_URL` turn it on. `INTERNAL_URL` is Grafana as the controller reaches it (`http://host:3000`, never published to users); `ADMIN_*` is a Grafana server administrator the controller manages organizations with (user default `admin`); `DS_SECRET` (at least 32 random characters, the same on every node) keys the per-tenant data source tokens; `DS_URL` is how *Grafana* reaches this controller's `/internal/ds` (not through the public front) |
| `SWITCHYARD_PROMETHEUS_URL`, `SWITCHYARD_LOKI_URL` | Metrics (and with Loki: access logs and p95) for the UI; see below |
| `SWITCHYARD_GITLAB_URL`, `_PROJECT`, `_TOKEN` | Git mirror of every revision. A fallback: settings saved in Admin > System settings > Git mirror replace them (see below) |
| `SWITCHYARD_SANDBOX=docker` | Validate configuration with `haproxy -c` in a container before pushing |
| `SWITCHYARD_BREAK_GLASS=1` | Allow the local break-glass administrator to log in |

## Logging in people

```bash
syctl idp add authentik --issuer https://sso.example.test/application/o/switchyard/ --client-id switchyard --client-secret-ref kv/idp/authentik
syctl idp map authentik switchyard-admins system-admin
syctl idp map authentik acme-admins tenant-admin --tenant acme
syctl idp list
```

Roles come from the group claim: a changed mapping applies within a minute without signing anyone out
(sessions last four hours, 30 minutes idle; `POST /auth/refresh` applies it at once), and a person with
no remaining grant is signed out. Administrators can end a person's sessions or disable them
(`/users/{id}/revoke-sessions`, `/disable`). See [auth.md](auth.md). The break-glass administrator is created with
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

- An apply that was rejected or reverted **fails** the job (`state: dead`, the reason in `error`, no retry; ADR 0223);
  read the failing stage. A `succeeded` apply job always has `ok: true`. `retrying: true` means an attempt failed and the job waits for its next try.
- `POST …/jobs/{id}/retry` (operator) queues a `dead` job again with a fresh attempt budget.
- `POST …/jobs/{id}/cancel` (operator) cancels a job that has not started; a running job cannot be cancelled.
  Cancelling an apply sets its change set to `failed`, so it can be applied again.

Both are audited. Progress is read by polling. See ADR 0016.

## Metrics and logs

The UI's charts and log tail read Prometheus and Loki through the controller, which adds the tenant to every
query. The telemetry pipeline must provide: Prometheus series from each SE's HAProxy exporter with a `tenant`
label (tenant id) and HAProxy's `proxy="vs_<service name>"`; Loki lines from the SE's HAProxy access log with the
labels `tenant` and `frontend="vs_<service name>"`. The lines carry a request id, the SNI and host, the upstream
address and port, retries, status, bytes and timings ([render.md](render.md#access-log)); those stay fields of the
event, not labels. Set `SWITCHYARD_PROMETHEUS_URL` (and `SWITCHYARD_LOKI_URL`).
Without them the metrics and log routes answer `available: false` and the UI shows "no data source". The
pipeline itself (Prometheus, Loki, Grafana, and the Vector log shipper on the SEs) is `deploy/telemetry/`; see ADR 0052
and `docs/runbooks/telemetry.md`.

## The controller's own metrics and SE discovery

`GET /metrics` (Prometheus text format, outside `/api/v1`) reports the job queue by kind and state, the run time of
finished jobs (`switchyard_job_duration_seconds_count`/`_sum`), change sets by state (`failed` are apply failures),
engines by reachability, engines with drift, drift findings and the age of the newest status poll
(`switchyard_status_age_seconds`, alert when it grows), plus one `switchyard_virtual_services` series per virtual
service. Values come from the database, so every replica answers alike.

Every one of those series carries `tenant` (the tenant id; `system` for jobs that belong to no tenant) and the
per-service series also carries the service's own name, so a per-tenant query returns that tenant's series and
`switchyard_virtual_services` joins to the objects it owns. The labels are bounded by the configuration, not by the
traffic: `tenant` comes from the tenant table, the rest from domain enums and the virtual-service table, and
per-request values (request id, host, SNI, upstream) never become labels — they live in the access log line.

`GET /api/v1/discovery/prometheus?exporter=haproxy|node&port=` is a Prometheus HTTP service-discovery list: one group
per enabled engine with a management address, labelled `tenant` (id), `tenant_name`, `se_group`, `engine` and `vs`
(service names between commas). Default ports: HAProxy exporter 8405, node exporter 9100. Authenticate both with
`SWITCHYARD_METRICS_TOKEN`:

```yaml
scrape_configs:
  - job_name: switchyard
    authorization: { credentials_file: /etc/prometheus/switchyard-token }
    static_configs: [{ targets: ["controller.example.test:8000"] }]
  - job_name: se-haproxy
    authorization: { credentials_file: /etc/prometheus/switchyard-token }
    http_sd_configs:
      - url: https://controller.example.test/api/v1/discovery/prometheus?exporter=haproxy
        authorization: { credentials_file: /etc/prometheus/switchyard-token }
```

The node exporter (9100) is part of the SE image. The HAProxy exporter (8405) is rendered into `haproxy.cfg` for SE groups with a `mgmt` network (ADR 0063).

## Audit log

`GET /tenants/{t}/audit-events` filters by actor, action (`change_set.*` style prefixes), object, time and text;
`GET /audit-events` (system administrators) spans tenants; `GET /audit-actions` lists every action name. A new
`action=` string in the code must be added to `switchyard/db/audit_actions.py` (a test enforces it).

## Housekeeping

An hourly job expires drafts untouched for 14 days, finished jobs after 30 days, stale engine status and
expired login or enrollment material. It never touches desired state, revisions or the audit log.

## End-to-end lab

`sh deploy/lab/e2e/run.sh` (about eight minutes; needs the dev PostgreSQL and OpenBao) deploys an LXC engine on a real
Proxmox through the controller, enrolls it, applies a virtual service and sends traffic through the VIP; `teardown.sh` removes
it. See ADR 0056.

## Testing against a Proxmox API

`docker compose -f deploy/lab/pve/compose.yaml up -d`, then `sh deploy/lab/pve/bootstrap.sh` prints the `SWITCHYARD_TEST_PVE_*`
variables; with them set, `pytest tests/integration/test_proxmox_live.py` runs the adapter against a real Proxmox VE (ADR 0054).

## Git mirror

Every applied revision can also be written as YAML to a GitLab repository, for review and recovery
([ADR 0216](adr/0216-git-mirror-settings.md)). PostgreSQL stays the source of truth: a mirror failure never blocks an apply, and the
write is retried.

Configure it in **Admin > System settings > Git mirror** (system administrators), or with the API:

```bash
curl -X PUT $URL/api/v1/system/git-mirror -d '{"enabled":true,"url":"https://git.example.com",
  "project":"ops/switchyard-mirror","branch":"main","token_ref":"kv/git/mirror-token"}'
curl -X POST $URL/api/v1/system/git-mirror/test      # reads the branch; writes nothing
```

- `token_ref` is an OpenBao reference to a project access token with write access to that repository only. Never enter the token itself.
  The controller's OpenBao policy must allow reading it (`syctl openbao-policy --platform kv/git/mirror-token`).
- Test reports whether the branch can be read with the stored credentials. The page also shows when and where the last write went, and its
  error if it failed.
- Settings are read at write time, so a change needs no restart. Saving settings replaces `SWITCHYARD_GITLAB_*`; until then those variables
  keep working and the page says so.
- While the mirror is off or unconfigured, mirror jobs finish without writing.

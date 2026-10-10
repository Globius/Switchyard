<!-- Snapshot exported 2026-10-10 from the online doc: https://claude.ai/code/artifact/3698c8a8-230c-4787-998c-007be6665190
     The online doc may be newer. Names updated to Switchyard (syctl, sy-agent). -->

# Switchyard — Backend Design

Oct 10, 2026 · @Miguel Rodriguez

## Summary

The backend is a Python API plus a pool of workers sharing one PostgreSQL database. A small Go agent runs on every SE VM. Every change is a versioned, previewable change set that workers render, validate and push to the SE groups. Companion to [NSX ALB (Avi) → Open-Source LB Replacement Plan](https://claude.ai/code/artifact/bf06390f-6cf2-4fa9-9128-b6e8286ecb93).

Key decisions:

- **Language and framework:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async) with Alembic migrations. The OpenAPI spec is generated from the models and drives the UI client and the CLI.
- **One database, no Redis:** PostgreSQL holds desired state, revisions, the job queue (`SELECT … FOR UPDATE SKIP LOCKED`), the audit log and the latest status. That leaves one stateful thing to back up and run in HA.
- **Tenancy enforced twice:** a `tenant_id` on every tenant-owned row, checked in the service layer and again by PostgreSQL row-level security.
- **Change sets, not live edits:** edits collect in a draft change set; Apply shows a rendered diff, then commits a new revision. Rollback re-applies an older revision.
- **Agent, not SSH:** HAProxy is driven through its Data Plane API; FRR, Keepalived, IPVS and Coraza are driven by an agent over mTLS. The controller never holds SSH keys to the SEs.
- **Secrets in OpenBao:** private keys and API tokens (Proxmox, Cloudflare) live in OpenBao; PostgreSQL only stores references to them.
- **Git as an audit mirror:** every committed revision is also written as YAML to a GitLab repo, for review and disaster recovery. PostgreSQL stays the source of truth.

## Components

Only the workers talk to SEs and external systems. The API reads and writes the database and enqueues jobs, so a slow Proxmox or Cloudflare call never holds an HTTP request open.

> Diagram (backend components · controller, SE group, external systems): see the [online doc](https://claude.ai/code/artifact/3698c8a8-230c-4787-998c-007be6665190).

## Data model

Desired state is stored as normal relational tables with foreign keys, so references (a VS to its pool, cert or VIP) can't dangle. Each committed change set produces an immutable revision snapshot.

### Tables

| Table | Key columns | Notes |
| --- | --- | --- |
| `tenant` | id, name, oidc\_group, quotas (jsonb) | Root of ownership; `system` tenant for shared objects |
| `network` | tenant\_id, sdn\_zone, vnet, cidr, gateway, vlan\_or\_vni, managed (bool) | `managed` = created by the controller vs. attached |
| `vip_pool` / `vip_allocation` | network\_id, ranges / pool\_id, ip, owner\_ref | Unique (pool, ip); allocation tied to a VS VIP |
| `se_group` | tenant\_id, template\_id, vm\_size, vm\_count, ha\_mode, routing\_profile\_id | `ha_mode` = `bgp` or `vrrp` |
| `se_group_network` | se\_group\_id, network\_id, role | Role = `vip`, `backend` or `mgmt`; drives VM NICs |
| `service_engine` | se\_group\_id, pve\_node, vmid, mgmt\_ip, agent\_cert\_serial, state | One row per VM; state from Proxmox + agent |
| `routing_profile` | tenant\_id, asn, bfd, vrrp\_vrid, priorities | Peers and static routes in child tables |
| `bgp_peer` / `static_route` | routing\_profile\_id, peer\_ip, remote\_asn, md5\_ref / prefix, next\_hop | `md5_ref` points into OpenBao |
| `virtual_service` | tenant\_id, se\_group\_id, name, protocol, enabled | Protocol = `http`, `tcp` or `udp` |
| `vs_vip` / `vs_port` | vs\_id, vip\_allocation\_id / port, tls, http2, proxy\_protocol | A VS can listen on several ports |
| `vs_route` | vs\_id, match (host, path), action (pool, redirect), order | SNI/host routing and redirects |
| `pool` / `pool_member` | tenant\_id, algorithm, persistence / pool\_id, address, port, weight, admin\_state | `admin_state` = enabled, disabled or drain |
| `health_monitor` | tenant\_id, type, interval, rise, fall, http (jsonb) | Shared across pools. Type = `tcp`, `http`, or `udp` (M6) |
| `certificate` | tenant\_id, source, sans, not\_after, secret\_ref, renew\_policy | Key material only in OpenBao |
| `policy_set` | tenant\_id, kind, rules (jsonb, schema-validated) | HTTP rules, allow lists, rate limits |
| `waf_policy` | tenant\_id, mode, paranoia, exclusions (jsonb) | Phase 2 |
| `change_set` | tenant\_id, author, state, summary, base\_revision | State = `draft`, `pending_approval`, `applying`, `applied`, `failed` or `discarded`; approver and approval time recorded |
| `revision` | tenant\_id, number, change\_set\_id, snapshot (jsonb), git\_sha | Immutable; source for rollback |
| `deployment` | revision\_id, se\_group\_id, state, rendered\_hash, error | One per SE group touched by a revision |
| `job` | kind, payload, state, run\_after, attempts, locked\_by | The job queue |
| `audit_event` | actor, tenant\_id, action, object\_ref, diff (jsonb), at | Append-only |
| `identity_provider` | name, protocol (oidc / saml), metadata or issuer URL, client\_secret\_ref, claim mapping, enabled | Several active at once; role mappings in a child table per tenant |

### Tenancy and edits

- **Tenancy:** every request sets `app.tenant_ids` on its DB session, and RLS policies limit rows to those tenants. System admins bypass RLS through a separate DB role, never by turning RLS off in code.
- **Draft edits:** a change set stores a list of operations (`change_op`: object type, id, create/update/delete, body, expected `version`), not shadow copies of tables. Preview applies the ops in memory on top of the latest state. Apply runs them in one DB transaction, and fails if any object's `version` has moved since. Every object row carries an integer `version`.
- **Object names:** unique per tenant. IDs are UUIDv7, so they sort by creation time.

## API design

The API is versioned REST under `/api/v1`, scoped by tenant in the path, and the OpenAPI spec is the contract for both the UI and the `syctl` CLI.

### Resources

| Path | Methods | Purpose |
| --- | --- | --- |
| `/tenants` | GET, POST, PATCH, DELETE | System admins only |
| `/tenants/{t}/networks`, `/vip-pools`, `/se-groups`, `/routing-profiles` | CRUD | Infrastructure objects |
| `/tenants/{t}/virtual-services`, `/pools`, `/health-monitors`, `/certificates`, `/policy-sets` | CRUD | Application objects |
| `/tenants/{t}/change-sets` | GET, POST | Open a draft; list drafts and history |
| `/tenants/{t}/change-sets/{id}/diff` | GET | Object diff + rendered config diff per SE group |
| `/tenants/{t}/change-sets/{id}/apply`, `/discard` | POST | Apply returns a job id |
| `/tenants/{t}/change-sets/{id}/request-approval, /approve, /reject` | POST | Optional four-eyes flow when the tenant requires approval |
| `/tenants/{t}/revisions`, `/revisions/{n}/rollback` | GET, POST | History; rollback opens a change set that restores revision n |
| `/tenants/{t}/pools/{id}/members/{m}/state` | PUT | Enable, disable or drain at runtime (no change set, audited) |
| `/tenants/{t}/status/...` | GET + SSE stream | Live VS, member, BGP and VRRP status |
| `/jobs/{id}` | GET | Async job progress and logs |
| `/certificates/{id}/renew`, `/se-groups/{id}/deploy` | POST | Long-running actions, return job ids |

### Conventions

- **Writes go through a change set:** `POST/PATCH/DELETE` on an object take a `?change_set=` id, or open an implicit single-op change set when the client asks for `?apply=true`. That keeps the CLI convenient without a second write path.
- **Draft reads:** every `GET` (single object and list) accepts `?change_set=<id>` and returns the state with that draft's ops applied in memory. Each object carries `draft_status` (`unchanged`, `modified`, `created`, `deleted`) and the list of changed fields, so the UI can mark them.
- **Optional approval (four-eyes):** a per-tenant `require_approval` setting (with an optional SE-group scope). When it's on, `POST …/change-sets/{id}/request-approval` freezes the draft as `pending_approval`; `…/approve` (which then applies it) and `…/reject` (with a comment, which reopens the draft) need the `approver` permission and a user other than the author. Any change to the ops voids the request. System-admin break-glass applies bypass approval with a mandatory reason. Every request, approval, rejection, bypass and toggle change writes an `audit_event` and goes to syslog.
- **Concurrency:** each object returns an `ETag` of its `version`. Updates need `If-Match`, and a stale version gets `412`.
- **Async:** anything that touches SEs, Proxmox, Cloudflare or ACME returns `202` with a job id; clients poll `/jobs/{id}` or subscribe to the SSE stream.
- **Errors:** RFC 9457 problem+json, with a field path for validation errors so the UI can mark the exact input.
- **Pagination and filtering:** cursor pagination (`?cursor=&limit=`) plus `?name=`, `?label=` and `?se_group=` filters.
- **Validation:** Pydantic for shape. A domain validator runs referential and network checks before render: VIP is inside the SE group's networks, ports don't collide on a VIP, cert SANs cover the VS hostnames.

### Auth and RBAC

- **Users:** any number of identity providers at once, each OIDC (authorization code + PKCE, via Authlib) or SAML 2.0 (via pysaml2). The first ones are Authentik and Entra ID. The login page lists the enabled providers. Each provider has its own claim or attribute mapping for username and groups, and its own group-to-role mapping per tenant. After login the API issues its own short-lived session, so the rest of the backend doesn't care which provider was used. A local break-glass admin (password + TOTP, disabled by default) covers an outage of every provider.
- **Machines:** per-tenant API tokens (hashed, scoped, expiring) for CI and automation.
- **Roles:** `system-admin`, `tenant-admin` (everything in a tenant, including SE groups and networks), `operator` (apps, certs, drain, apply), `viewer`. A role grants a verb on an object type within a tenant. The check runs as a FastAPI dependency on every route, and RLS backs it up in the database.

## Change pipeline

Applying a change set renders, validates, tests on one SE, then rolls out. Every stage before the canary leaves the data plane untouched, and a bad canary is reverted before any other SE sees the change.

> Diagram (apply pipeline · render, validate, canary, roll out): see the [online doc](https://claude.ai/code/artifact/3698c8a8-230c-4787-998c-007be6665190).

- **Lock:** a PostgreSQL advisory lock per SE group, so two change sets never deploy to the same group at once. Change sets touching different groups run in parallel.
- **Render:** pure functions from the revision snapshot to files: `haproxy.cfg` (Jinja2 templates per object type), `crt-list`, map files for SNI host routing, `frr.conf`, `keepalived.conf` (VRRP plus IPVS virtual servers), and Coraza directives. The output is deterministic and sorted, so the same state always yields the same bytes and hash.
- **Validate:** `haproxy -c`, `vtysh --dryrun` and `keepalived --config-test` run in a sandbox container with the same versions as the SE image, plus the domain checks from the API layer.
- **Push:** the rendered \`haproxy.cfg\` is pushed whole through the Data Plane API raw-configuration endpoint (version-checked, so a concurrent edit aborts), followed by a hitless reload. Server state changes go through the Runtime API with no reload. Everything else goes to the agent as a signed bundle, which writes it atomically and reloads only the daemons whose files changed.
- **Canary check:** for 30 s by default (set per SE group), confirm the HAProxy process is up and its config hash matches, VIPs are still announced (BGP session up or VRRP state as expected), and no backend went from up to down because of the change.
- **Commit:** write the revision, the `deployment` rows with rendered hashes, and the audit event. Then queue a job that writes YAML to GitLab.
- **Rollback:** opens a change set that restores revision n and runs the same pipeline. There is no special fast path, so rollbacks are validated too.
- **Drift:** a periodic job compares each SE's reported config hashes with the expected ones. A mismatch raises an alert and shows in the UI, and is never auto-corrected, so a manual hotfix is noticed rather than erased.

## Southbound adapters and the SE agent

Each external system sits behind one Python adapter with a typed interface and a fake for tests, so the pipeline and API never call a vendor client directly.

| Adapter | Talks to | Used for | Notes |
| --- | --- | --- | --- |
| `haproxy` | HAProxy Data Plane API v3 on each SE (mTLS) | Raw config push with version check, reload status, Runtime API (server state, weights, cert hot-swap), stats | Per-SE client; retries are idempotent because pushes carry the config version |
| `agent` | Switchyard agent on each SE (gRPC over mTLS) | FRR, Keepalived/IPVS and Coraza bundles; BGP, VRRP and daemon status; config hashes; tcpdump jobs later | See below |
| `proxmox` | Proxmox VE API (scoped API token) | Clone the SE template, cloud-init, NICs, start/stop/destroy; SDN zones, VNets and subnets, then apply SDN | SDN apply is cluster-wide, so it is serialized behind its own lock |
| `cloudflare` | Cloudflare API (scoped tokens per zone) | DNS-01 TXT records, Origin CA certs, DNS records for VS hostnames, published IP ranges | TTL at least 60 s for TXT records |
| `acme` | ACME CA (Let's Encrypt, step-ca) | Order, DNS-01 via the `cloudflare` adapter, finalize | Python `acme` library; issue once per cert, never per SE |
| `secrets` | OpenBao KV v2 + PKI | Private keys, tokens, BGP MD5 keys; agent and Data Plane API client certs | Controller authenticates with AppRole |
| `git` | GitLab repo over HTTPS (deploy token) | YAML mirror of each revision | Async; failure alerts, never blocks an apply |
| `metrics` | Prometheus and Loki HTTP APIs | Graphs and log tails for the UI | Read-only |

### SE agent

- **What it is:** a single static Go binary (`sy-agent`) baked into the SE template and run by systemd. Go gives one file with no runtime to patch on the SE; the controller stays Python.
- **Enrollment:** cloud-init gives the agent a one-time token. The agent trades it for a client cert from OpenBao PKI through the controller, then connects with mTLS. Certs are short-lived (24 h) and renewed automatically.
- **API:** gRPC with `ApplyBundle`, `GetStatus` and a `Watch` stream for status changes. Bundles are signed by the controller and written atomically (temp file, then rename). The agent reloads only daemons whose files changed, and keeps the last good bundle for local restore.
- **What it reports:** HAProxy process state and config hash, BGP sessions (from `vtysh -c 'show bgp summary json'`), VRRP state per instance, IPVS tables, disk and memory.
- **Route health injection:** a local watchdog in the agent withdraws BGP prefixes, or lowers VRRP priority, when HAProxy's local health endpoint fails. Failover never waits on the controller.
- **Scope limits:** the agent only writes files under `/etc/switchyard/` and can only reload a fixed list of units. There is no remote shell or arbitrary command endpoint.

## UDP virtual services (IPVS, M6)

UDP is a phase 2 feature (M6+). The data model, validators and the port-collision check already know `protocol: udp`; what is missing is the Keepalived/IPVS renderer, the agent's IPVS status and the monitor type below. Decisions are recorded in [ADR 0013](../adr/0013-udp-keepalived-ipvs.md).

- **Forwarding mode:** IPVS NAT (`lb_kind NAT`) plus source NAT (masquerade) on the SE, so backends need no change: no VIP on the backend, no route back through the SE. The agent bundle also sets `net.ipv4.ip_forward=1` and `net.ipv4.vs.conntrack=1` and ships a small nftables masquerade rule for traffic IPVS sends to real servers. Cost: backends see the SE's address, not the client's, and all traffic crosses the SE in both directions. Direct routing (DR) is rejected because it needs the VIP on every backend's loopback with ARP suppression and L2 adjacency to the SE. In a multi-tenant setup the platform does not control tenant backends, and one tenant's VIP would have to be configured on machines it cannot see.
- **Health checks:** a pool monitor of the new type `udp` renders `UDP_CHECK` (a probe that marks the server down on ICMP port-unreachable, optionally with a payload and an expected reply). `MISC_CHECK` (protocol-aware probes such as DNS or RADIUS) is not exposed in M6, because it runs a script on the SE; later it can ship as fixed, agent-provided probes under their own types (`dns`, `radius`), never as free-form scripts. **The `MonitorType` enum gains `udp`.** Allowed pairs: `udp` monitors only on pools used by UDP services, and `tcp`/`http` monitors only on pools used by `tcp`/`http` services. The `http` block stays forbidden for `udp`. A monitor `port` is optional; otherwise the member's port is probed. Member `admin_state`: `drain` renders `weight 0`, `disabled` omits the real server.
- **Scheduler mapping:** `roundrobin`→`rr`, `leastconn`→`lc`, `source`→`sh`. When members have unequal weights the renderer uses `wrr` / `wlc` instead, so weights are honoured. **`random` is not allowed on UDP services**: IPVS has no random scheduler, and a silent substitute would change behavior. The snapshot validator rejects it on any pool used by a UDP service. `sh` is the choice for flows that must stay on one backend (WireGuard), because it needs no connection-table state to pick the same server.
- **HA:**
  - *VRRP mode:* as today. `keepalived.conf` holds the `vrrp_instance` and the `virtual_server` blocks together, so the master serves the VIP and the backup holds the same IPVS table, ready to take over.
  - *BGP mode:* Keepalived runs for IPVS only (no `vrrp_instance`) and FRR still advertises the VIP /32. The agent watchdog withdraws the route when `keepalived` is down or a UDP service has no healthy real server, as it already does for HAProxy. The upstream routers spread flows over the SEs by hash (ECMP).
  - *Connection sync:* **`lvs_sync_daemon` is not used in M6.** It needs a `vrrp_instance`, so it does not fit BGP mode, and a single master-to-backup stream does not fit active/active. Without it a failover or an ECMP rehash (SE added or removed) can send an in-flight UDP flow to an SE with no table entry. With `sh` that SE still picks the same backend; with `rr`/`lc` it may pick another, which is harmless for DNS or syslog. Revisit with VRRP-only sync if a real workload needs it.
- **Fields on a UDP virtual service:** `vip`, `ports` (number only) and `default_pool_id`, plus the common `name`, `enabled`, `se_group_id`. No TLS, `proxy_protocol`, `http2`, `https_redirect_port`, routes, policy sets, allow list, `restore_client_ip_header` or certificates. On a UDP service's pool: no `tls`, no `persistence`, no `random`. The validator today (`domain/models.py`, `domain/snapshot.py`) already rejects `tls`, `proxy_protocol`, routes, policy sets, `http2`, `https_redirect_port` and `restore_client_ip_header`, and requires `default_pool_id`. **Still to add:** reject `certificate_ids`, `allow_list_id` and `enforce_allow_list`; reject pools with `tls` or `persistence` or `random`, or with a `tcp`/`http` monitor, when a UDP service uses them; reject a pool shared by a `tcp` and a `udp` service (`pool_protocol_mismatch` only separates `http` from `tcp`/`udp` today); require a port on every member of a pool used by UDP. Also, today's checks raise one model-level message with no field path; each rule should report its field (`ports[0].tls`, `certificate_ids`) in `errors[].path`.
- **Validation:** the `keepalived.conf` output must pass `keepalived -t` in a testcontainer with the SE image's Keepalived version, in the same golden-file tests that run `haproxy -c` (`tests/integration/test_golden_haproxy.py`), with no warnings allowed. `-t` checks syntax only; a real IPVS test needs the host's `ip_vs` module and a privileged container, so it runs as a separate, optional integration lane for the agent.
- **Status:** `sy-agent` reads the IPVS table over netlink (the IPVS generic-netlink family), falling back to `ipvsadm -Ln --stats --rate` where netlink is unavailable. It reports each real server in the same member shape as HAProxy (state up/down/drain, weight, active and inactive connections, packet and byte counters), keyed by virtual service and member, so `poll_status`, SSE and the UI need no UDP-specific branch.
- **Until M6:** a UDP virtual service is **accepted and stored** like any other and takes part in validation and the port-collision check, but nothing serves it. The HAProxy renderer skips it with a warning (`docs/render.md`). That warning now also reaches the API: the change-set diff gets a `warnings[]` list next to `problems[]` with `{code: "udp_not_served", path, message: "UDP not served until M6"}`. It is a separate list because a `problem` blocks the apply; a warning does not. The engine status for such a service reports `state: "not_served"`, so the UI can say so instead of showing it as down. Tickets: T6 (now) and T7 (M6) in [05-frontend-backend-tickets.md](05-frontend-backend-tickets.md).

## Background jobs, status and telemetry

Workers pull jobs from the PostgreSQL `job` table; status lives in the database for the UI, and time series live in Prometheus.

### Jobs

| Job | Trigger | Notes |
| --- | --- | --- |
| `apply_change_set` | API apply / rollback | Runs the change pipeline |
| `deploy_se_group` | API deploy, VM count change | Clone and boot VMs, wait for agent enrollment, push the current revision, then add the VMs to the group |
| `apply_sdn` | Network created or changed | Serialized cluster-wide |
| `issue_cert` / `renew_certs` | API request / daily | Renews at 30 days before expiry, then pushes through Runtime API cert hot-swap (no reload) |
| `sync_cloudflare_ips` | Daily | Updates the Cloudflare-only allow list; it goes through a normal change set so the change is audited |
| `poll_status` | Every 10 s per SE | Pulls Runtime API stats and agent status; writes `se_status` and `member_status` |
| `detect_drift` | Every 5 min | Compares reported config hashes with the expected ones |
| `git_mirror` | After each commit | Retries with backoff |
| `gc` | Hourly | Expires old drafts, job rows and stale status |

- **Queue mechanics:** a worker claims a job with `SELECT … FOR UPDATE SKIP LOCKED`. Jobs are idempotent, have an attempt limit with exponential backoff, and send heartbeats so a crashed worker's job is reclaimed.
- **Scheduling:** periodic jobs are scheduled by whichever worker holds a PostgreSQL advisory lock as leader. No separate cron service is needed.

### Status and telemetry

- **Live state:** `poll_status` writes the latest state (up/down/drain, sessions, BGP/VRRP) into small status tables, and `LISTEN/NOTIFY` fans changes out to the API. The API streams them to the UI over SSE.
- **Metrics:** HAProxy's Prometheus exporter and node exporter on every SE, scraped by Prometheus. Scrape targets come from a controller HTTP service-discovery endpoint, so new SEs appear automatically with `tenant`, `se_group` and `vs` labels.
- **Logs:** HAProxy logs to local syslog, Vector ships them to Loki labelled the same way. Request logs keep the timing fields (Tq/Tw/Tc/Tr/Tt) for the latency breakdown view.
- **Controller metrics and logs:** the controller exposes its own Prometheus metrics (job durations, apply failures, drift count) and structured JSON logs with a request id that flows into jobs.

## Security, HA and deployment

The controller can create VMs and networks, so treat it like a hypervisor management plane: separate management network, least-privilege credentials, everything audited.

### Security

- **Least privilege upstream:** the Proxmox token gets a custom role limited to the SE pool and the SDN zones the controller manages. Cloudflare tokens are per zone with DNS:Edit (plus SSL and Certificates for Origin CA). The GitLab deploy token can write only to the mirror repo.
- **Secrets:** nothing secret goes in PostgreSQL, logs or the Git mirror; YAML exports keep only `secret_ref`s. Private keys reach the SE only inside the HAProxy cert push or a signed agent bundle.
- **Transport:** mTLS from the controller to every Data Plane API and agent. Each SE gets its own client cert, and each Data Plane API trusts only the controller's CA.
- **Input safety:** every user value that reaches a config file passes a strict validator (hostnames, paths, regex, header names) and is escaped by the renderer; `haproxy -c` is not the only guard. No free-form config snippets in phase 1. This is the class of issue behind Roxy-WI's SSRF.
- **Audit:** every API write, apply, rollback, drain and login goes into `audit_event` and to syslog.
- **Web:** the UI uses a cookie session with SameSite=Strict plus a CSRF token; API tokens use bearer auth only; strict CSP.

### HA and deployment

- **Packaging:** one container image with two entrypoints (`api`, `worker`), plus the agent as a .deb in the SE template.
- **Run on:** three controller VMs in a dedicated `management` tenant on this platform, each running api, worker, PostgreSQL/Patroni and etcd under Docker Compose. The controller VIP comes from the management tenant's own SE group.
- **Bootstrap:** the first three VMs and the management SE group are built by OpenTofu + cloud-init. The controller then adopts them as existing objects (an `adopt` import), so from then on they appear in the UI like any other tenant.
- **Break-glass:** the `management` tenant is locked. Changes need `system-admin` plus a confirmation, and the pipeline refuses any change that would remove the controller VIP or the SE group serving it. Each controller VM is also reachable on its management IP directly, so recovery never depends on the controller's own VIP.
- **Database:** PostgreSQL 17 with Patroni across the three controller VMs from day one (etcd for the quorum, synchronous replication to one standby). Back it up with pgBackRest to object storage. Restore drills go in the runbook.
- **Interim placement (two Proxmox hosts):** controller VMs 1 and 2 run on different Proxmox hosts, kept apart by a Proxmox HA anti-affinity rule. VM 3 runs on vSphere/ESXi with etcd and a Patroni replica tagged `nofailover: true`, so losing either Proxmox host still leaves a 2-of-3 quorum and a Proxmox-hosted primary. VM 3 sits outside the `management` tenant and is shown in the UI as an external member. When a third Proxmox host arrives: deploy a new controller VM there, let it join etcd and Patroni and sync, then remove the ESXi member (`etcdctl member remove`, `patronictl remove`). There is no downtime and no config change on the other nodes.
- **Statelessness:** the API is stateless, and workers coordinate only through the database, so any instance can die. If the whole controller is down, SEs keep serving traffic and keep failing over locally; only changes stop.

## Repo layout, testing and documentation

One monorepo in GitLab; the layers depend downward only (api → services → domain), so render and validation logic can be tested without a database or network.

```text
switchyard/
  controller/
    switchyard/api/          FastAPI routers, auth, RBAC dependencies, SSE
    switchyard/domain/       Pydantic models, validators (no I/O)
    switchyard/services/     change sets, revisions, IPAM, tenancy rules
    switchyard/render/       Jinja2 templates + renderers (pure functions)
    switchyard/pipeline/     apply, canary, rollback, drift
    switchyard/adapters/     haproxy, agent, proxmox, cloudflare, acme, secrets, git, metrics
    switchyard/jobs/         queue, scheduler, job handlers
    switchyard/db/           SQLAlchemy models, RLS policies, Alembic migrations
    tests/            unit, golden, integration
  agent/              Go: sy-agent, protobufs, tests
  proto/              gRPC definitions shared by controller and agent
  ui/                 React + TypeScript (separate design)
  se-image/           cloud-init, OpenTofu, package lists
  deploy/             Compose files, Patroni, example configs
  docs/               architecture, API, runbooks, ADRs
```

### Testing

- **Unit (pytest):** domain validators, IPAM allocation, RBAC rules, change-set conflict detection. Hypothesis property tests for IPAM and rendering (for example, rendering is deterministic and every VS hostname appears in exactly one map).
- **Golden files:** for each renderer, sample state goes in and the exact `haproxy.cfg`, `frr.conf` and `keepalived.conf` come out, then the output is validated by the real binaries in CI. Any change to rendered output shows as a reviewable diff.
- **Adapter contract tests:** run against real HAProxy + Data Plane API, FRR and Keepalived containers (testcontainers); Proxmox and Cloudflare use recorded fixtures plus fakes.
- **Database tests:** run against a real PostgreSQL. They include RLS tests proving that tenant A's token cannot read or write tenant B's rows through any route.
- **Agent (Go):** unit tests for atomic writes and unit allowlists; integration tests in a systemd container.
- **End to end:** a lab Proxmox cluster where CI deploys a real SE group, creates a VS, and checks traffic, canary failure handling, rollback and failover.
- **Gates:** ruff, mypy (strict), go vet, 85% line coverage on `domain`, `render` and `pipeline`, and OpenAPI breaking-change checks.

### Documentation

- Docstrings on every public function, especially renderers and adapters. `docs/` holds architecture, the data model, the generated API reference, ADRs for each key decision in this doc, and runbooks (restore DB, rotate CA, replace an SE, emergency rollback).

## Build milestones and open questions

Build order runs from the core outward. Each milestone produces something usable, so the hand-written config and the UI never block each other.

1. **M1 — Core and render:** domain models, DB schema with RLS, tenants, pools, monitors, virtual services, certs (import only), renderers with golden tests. Output: `syctl render` produces a valid `haproxy.cfg` from the DB.
2. **M2 — Change sets and push:** change sets, diff, apply pipeline against a manually built SE pair through the Data Plane API, revisions, rollback, audit, Git mirror.
3. **M3 — Agent and routing:** `sy-agent`, enrollment, routing profiles (VRRP first, then BGP), the route-health watchdog, status polling and SSE.
4. **M4 — Infrastructure from the UI:** Proxmox adapter, networks and SDN, VIP pools/IPAM, SE group deploy from the template.
5. **M5 — Certificates and Cloudflare:** ACME DNS-01, Origin CA, renewal jobs, the Cloudflare-only allow list. This completes phase 1.
6. **M6+ — Phase 2:** UDP via IPVS (see UDP virtual services above), Coraza WAF, rate limits, Kubernetes connector, health scores, scale-out.

### Open questions

- [x] Agent in Go (single binary on SEs) or Python (one language for the team)? This doc assumes Go.
  - [x] Pros/cons?
  - [x] **Go:** one static binary, no runtime to patch on the SEs, small memory use, solid gRPC and netlink libraries. Cost: a second language and CI toolchain. **Python:** one language and shared models with the controller. Cost: a Python runtime on every SE to patch and pin, a bigger footprint, harder packaging. The gRPC contract keeps either swappable. Recommendation: Go, unless you would rather maintain a single language; the agent is small (\~2k lines) either way.
  - [x] Let's do Go.
  - [x] Go it is; the agent sections already assume it.
- [x] PostgreSQL: 3-node Patroni from day one, or a primary/replica pair until more tenants arrive?
  - [x] What's the difficulty from going from pri/rep to 3-node later on? Should this be done at the beginning?
  - [x] Adding a node to Patroni later is easy: a new replica clones itself and joins with no downtime. The hard move is plain streaming replication to Patroni, which needs a maintenance window and failover rework. Automatic failover also needs a 3-member etcd quorum either way. Since the controller already runs on 3 VMs, run Patroni + etcd on all three from day one at no extra hardware cost. HA section updated.
  - [x] Let's do 3 nodes, however, I only have 2 Proxmox hosts at this time. We could have one live in vSphere/ESXi until we get a 3rd Proxmox host.
  - [x] That works; the third node only needs network reach to the other two. Plan added to the HA section as Interim placement: one controller VM per Proxmox host (anti-affinity) plus a third on ESXi tagged `nofailover`, so it votes and replicates but never becomes primary. When the third Proxmox host arrives, add a member there, let it sync, then remove the ESXi one, with no downtime.
- [x] Where do the controller VMs live: their own management tenant on this platform, or outside it to avoid a circular dependency?
  - [x] Their own management tenant.
  - [x] Done; the bootstrap and break-glass plan is in the HA section.
- [x] Is WireGuard on IPVS needed in phase 1 (pull UDP into M3), or can it wait for phase 2?
  - [x] It can wait.
  - [x] Done: UDP and the WireGuard rebuild are phase 2 (M6) in both docs; the UDP design is in its own section and ADR 0013.
- [x] Which OIDC provider: the existing Keycloak, Authentik or Entra ID?
  - [x] Support any OIDC/SAML provider. We'll tie it to Authentik (and Entra ID if it allows for more than one IDP)
  - [ ] Done: multiple identity providers (OIDC and SAML) at once, each with its own group-to-role mapping. Auth section and data model updated.

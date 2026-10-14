# Production guide

How to run Switchyard for real. [quickstart.md](quickstart.md) gets a test instance up; this page says what to change and
what to watch. It points at the runbooks, ADRs and design documents that hold the detail instead of repeating them, and it
says where something has **not** been proven yet. Read [design/02-backend-design.md](design/02-backend-design.md)
("Security, HA and deployment") first.

## 1. Shape of a production install

- **Controller:** three VMs in a dedicated `management` tenant, each running the API, the worker, PostgreSQL with Patroni
  and etcd under Docker Compose ([adr/0048](adr/0048-production-deployment-shape.md), `deploy/prod/`). The controller's own
  VIP comes from the management tenant's SE group. Each VM is also reachable on its management address, so recovery never
  depends on the controller's own VIP.
- **Or on Kubernetes:** the same controller from a Helm chart with CloudNativePG instead of Patroni and etcd, in
  [Deploying on Kubernetes](deploy-kubernetes.md) (section 2 lists what is and is not proven).
- **Management tenant lock:** applying a change in `management` needs a system administrator and a reason, and the
  `controller` service and its SE group cannot be deleted or disabled ([adr/0051](adr/0051-management-tenant-lock-and-adopt.md)).
  Bring the first VMs in with `syctl adopt` (`deploy/prod/adopt/management.example.yaml`).
- **Two Proxmox hosts today:** put controller VMs 1 and 2 on different hosts and the third on another platform with its
  Patroni member tagged `nofailover`, as described in the backend design. List it under `controller_members` in the adoption
  file so the UI shows it as an external member ([controller settings](controller-settings.md#cluster-members-including-the-one-on-esxi)).
- **Engines:** HAProxy SEs cloned from the template in `se-image/`, placed by VRRP or BGP per SE group. SEs keep serving and
  failing over when the controller is down; tests in [adr/0056](adr/0056-end-to-end-lab.md) show a VIP surviving both the
  controller being stopped and the engines rebooting.
- **Monitoring** runs apart from the controller ([section 10](#10-monitoring)).

## 2. Deployment options for the controller

The controller stack is the same containers in every option: `api`, `worker`, a one-shot `migrate`, the `proxy` (nginx with
the built UI), PostgreSQL, and OpenBao next to it. Engines are always Proxmox guests (section 9); only where the controller
runs changes.

| Option | What exists on main | Status |
| --- | --- | --- |
| [Docker Compose](#docker-compose) | `deploy/prod/compose.yaml`: etcd, Patroni (PostgreSQL 17), `migrate`, `api`, `worker`, `proxy`, one copy per node | supported; run as three nodes over two Proxmox hosts with the controller VIP on its own SE group ([lab/2026-10-13-prod-deploy](lab/2026-10-13-prod-deploy/index.md)) |
| [VMs or LXC containers on Proxmox](#vms-or-lxc-containers-on-proxmox) | the Compose option inside three Proxmox guests, adopted into the `management` tenant | VMs: the reference shape (ADR 0048), one VM run. Privileged LXC: three nodes, failover and a reboot proven |
| [Kubernetes](#kubernetes) | a Helm chart with CloudNativePG (ADR 0191, !132) | proven on a lab k3s; see the limits below |

### Docker Compose

Three Linux hosts with Docker, each running `deploy/prod/compose.yaml` with its own `node.env`; steps in
[deploy/prod/README.md](../deploy/prod/README.md). Patroni keeps one synchronous standby, so a node can fail without losing a
committed change. The worker runs on every node; scheduled jobs take a leader lock in PostgreSQL, so they run once.
Put TLS in front of the three `proxy` ports (8080), ideally the management SE group (next option). `compose.lab.yaml` runs the
same stack on one host to rehearse failover and restore (`deploy/prod/lab-drill.sh`).

A single node (`deploy/quickstart/`) is fine for a lab or a small site that accepts downtime and restores from backup; it is
not HA and its OpenBao is a dev-style single node.

### VMs or LXC containers on Proxmox

The reference production shape: three VMs, each running the Compose option, in the dedicated `management` tenant, with the
controller's own VIP served by that tenant's SE group (section 1). Steps:

1. Create three VMs (section 4 for sizing), two on different Proxmox hosts and the third on another platform with its Patroni
   member tagged `nofailover` when you have only two hosts.
2. Install Docker and run the Compose option on each.
3. Bring the VMs and the controller's service under management with `syctl adopt`
   (`deploy/prod/adopt/management.example.yaml`); `syctl serve` already created the empty `management` tenant (ADR 0155).
4. Set the public address under Admin, Controller ([controller-settings.md](controller-settings.md)).

**LXC containers.** The Compose stack runs inside Proxmox LXC containers, and this has been proven in the lab, including a
reboot. The container must be **privileged** with `nesting=1` and `keyctl=1` (Docker needs both), and Docker networks on an
overlay need an explicit MTU (`--mtu`, or `com.docker.network.driver.mtu` in Compose) below the overlay's. Unprivileged
containers are not supported for the controller.

### Kubernetes

A Helm chart installs the controller in a cluster: `api`, `worker` and `proxy` Deployments (two replicas each in the lab),
a `migrate` Job per release revision, and PostgreSQL through the CloudNativePG operator. OpenBao stays outside the chart.
Install steps, values and secrets: [Deploying on Kubernetes](deploy-kubernetes.md); decision:
[ADR 0191](adr/0191-kubernetes-deployment.md); lab run: [Kubernetes install results](lab/2026-10-12-kubernetes-install.md).

Proven on a lab k3s: install with two replicas each, the controller deploying and enrolling an LXC engine, a virtual
service answering 200, sign-in and the UI, exposure through a `LoadBalancer` (k3s `servicelb`) and a CloudNativePG failover
(about 30 s to a new primary). **Not proven yet:** exposure through an Ingress, behaviour during an OpenBao outage, and an
upgrade that includes a schema migration. Treat those as pilot items.

Do not confuse this with the [Kubernetes connector](kubernetes.md), which publishes Services of an existing cluster through
Switchyard engines and works with any deployment option.

### Securing the database tier

The three controller nodes talk to each other on etcd (2379 clients, 2380 peers), Patroni's REST API (8008) and PostgreSQL
(5432). `deploy/prod/compose.yaml` ships these hardened, and a fresh install needs these steps first (they are why
`node.env` has `NODE_IPS`, `PKI_DIR`, `PATRONI_API_PASSWORD` and `DOCKER_SUBNET`):

1. **A certificate per node** from the OpenBao PKI role `internal` (server and client use, IP SANs allowed):
   `NODE_NAME=ctl-1 NODE_IP=10.0.0.11 sh deploy/prod/issue-node-cert.sh` writes `secrets/pki/{ca.crt,node.crt,node.key}`; copy the
   directory to the node (`node.key` must be readable by uid 999, the `postgres` user in the Patroni image; the script does it).
2. **etcd** listens on https only and demands a client certificate from the CA for clients and peers
   (`--client-cert-auth`, `--peer-client-cert-auth`); Patroni reaches it with the node certificate.
3. **Patroni's REST API** is https, listens on the node address only, and changes (switchover, restart, reload) need the
   password `PATRONI_API_PASSWORD`. `GET /cluster`, which the controller reads for Admin, Controller, stays open to anyone who
   reaches the port, so the firewall below matters; the controller verifies the certificate with `SWITCHYARD_PATRONI_CA`
   (set in `compose.yaml`) and the members' `patroni_url` is `https://`.
4. **PostgreSQL** has `ssl=on`, listens on the node address and loopback, and `pg_hba` admits only `hostssl` connections from
   the three node addresses and from the stack's own Docker network (`DOCKER_SUBNET`, fixed so it can be named), everything
   else is rejected. The controller connects with `sslmode=verify-full` against the same CA.
5. **A host firewall** (`sh deploy/prod/firewall.sh --persist`, nftables) lets only `NODE_IPS` and the Docker network reach
   2379, 2380, 8008 and 5432. It adds its own table and leaves other rules alone.

What this does not do: the certificates are not rotated for you (the script issues one year; re-run it and restart etcd and
Patroni one node at a time before they expire), and any holder of a certificate from that CA can reach etcd, so keep the PKI
role for nodes separate from the one for tenants if you share the OpenBao.

Proof, on the three-node lab with this configuration (3 LXC containers over two Proxmox hosts): a probe from a machine that is
not a node was refused on all four ports and reached the proxy on 8080; etcd answered a request with the node certificate and not
without; Patroni's REST API refused plain http and a switchover without the password (401); `pg_stat_ssl` shows the replicas
streaming over TLS. Stopping the leader's Patroni promoted the sync standby in under 5 seconds while the controller VIP kept
answering `/healthz`; the stopped node rejoined as a replica. A manual `patronictl switchover` worked, and
the node tagged `nofailover` was not promoted when the leader was stopped.

### Gotchas seen in the lab

The full list from the fresh three-node install, with timings and screenshots, is in
[lab/2026-10-13-prod-deploy](lab/2026-10-13-prod-deploy/index.md). The ones that bite first:

- The nodes may not be able to build images: build once and `docker load` (set `CONTROLLER_IMAGE`, `PROXY_IMAGE`).
- On an overlay or tunnel with an MTU below 1500 set `DOCKER_MTU` in `node.env`, or the controller cannot reach the engines.
- pgBackRest speaks only TLS to S3: use a store with TLS (`BACKUP_S3_VERIFY_TLS=n` for a self-signed lab one). Starting a new
  cluster against an old repository needs a new `BACKUP_REPO_PATH`.
- `syctl adopt` creates no revision: the first apply in the locked `management` tenant is a draft change set with a
  break-glass reason. Break-glass is off in production until a system administrator enables it.
- Keep `connect_timeout` and the TCP keepalive options in the database URL (the Compose file has them, 3 s): without them a database node that
  disappears freezes every request of the other nodes, `/healthz` included, for about two minutes. With your own PostgreSQL on Kubernetes
  put them in `database.external.options`.
- Give engines addresses nothing else uses; re-enrolling an engine needs `tls.crt`, `tls.key` and `renew.token` removed and
  `dataplaneapi` restarted.

- A proxy configured for the Docker **client** (`~/.docker/config.json` `proxies`) is injected into every container and breaks
  the API's calls to OpenBao. Remove it, or add the OpenBao address to `noProxy`.
- `deploy/quickstart/bootstrap.sh` reuses the existing UI image; pass `--rebuild` after updating the source.

## 3. Network and ports

| From | To | Port | Why |
| --- | --- | --- | --- |
| Browsers, `syctl`, automation | proxy (behind the management VIP) | 443 (8080 on each node behind TLS) | UI and API |
| Engines | controller | 443 (8443 in the quickstart) | enrolment and agent certificate renewal; https only |
| Controller | engine management address | 7443 | `sy-agent` (gRPC over mTLS): bundles, status |
| Controller | engine management address | 5556 | HAProxy Data Plane API (mTLS) |
| Prometheus | engine management address | 8405, 9100 | HAProxy and node exporters (management network only) |
| Engines | Loki | 3100 | logs from Vector |
| Controller | Proxmox API | 8006 | deploy, retire, placement, discovery |
| Controller | OpenBao | 8200 | secrets and agent PKI |
| Controller nodes | each other | 5432, 8008, 2379, 2380 | PostgreSQL, Patroni, etcd |
| Engines | engines of the group | VRRP (protocol 112), 10000 | failover, persistence peers |
| Engines | upstream routers | 179, BFD 3784 | BGP mode |
| Engines | CAPTCHA provider, ACME CA | 443 outbound | only if those features are used |

Keep the management network separate from the VIP and backend networks; the exporter refuses requests that do not arrive on
a management address. The controller never opens SSH to an engine. Engines need no internet access to start.

## 4. Sizing

Sizes are T-shirt sizes. What was **measured** is marked as such ([load test of 2026-10-14](lab/2026-10-14-load-test/index.md), on a lab
with 2 vCPU engines and nodes over a VXLAN overlay); the rest is **estimated** by scaling that, and should be checked with your own traffic
before you rely on it. All vCPU counts are virtual CPUs on a host that is not oversubscribed (the lab showed 8 to 15 % steal on a busy host).

### Engines (HAProxy VMs)

Measured on the 2 vCPU engine: it carried **about 5 000 plain-HTTP requests per second** (small objects, keep-alive, 128 connections) before
both vCPUs were busy (HAProxy 140 % of 200 %), so about **2 500 requests per second per vCPU**, and used 380 MB of its 2 GB (HAProxy itself 35 MB).
Plan to use at most 70 % of that. With VRRP the master carries all the traffic of its VIPs and the backup waits, so size one engine for the whole group;
with BGP the group's traffic spreads over its engines (ECMP) and each engine needs its share plus the share of one engine that fails.

| Size | vCPU | RAM | Disk | Plain HTTP, planned (70 %) | Concurrent connections | Status |
| --- | --- | --- | --- | --- | --- | --- |
| XS | 1 | 1 GB | 8 GB | 1 200 requests/s | 5 000 | estimated; labs and small internal services |
| S | 2 | 2 GB | 8 GB | 3 500 requests/s | 20 000 (the default `maxconn`) | **measured** (5 000 at saturation); the deploy default |
| M | 4 | 4 GB | 8 GB | 7 000 requests/s | 40 000 | estimated |
| L | 8 | 8 GB | 8 GB | 14 000 requests/s | 80 000 | estimated |
| XL | 16 | 16 GB | 8 GB | 28 000 requests/s | 160 000 | estimated; above this add engines (BGP) instead |

TLS termination costs more CPU per new connection than the plain-HTTP figures above and **was not measured**: with many short TLS connections
expect a fraction of those rates, and test with your certificates and key type (ECDSA is much cheaper than RSA). RAM: 1 GB plus about 1 GB
per 20 000 concurrent TLS connections. Raise `maxconn` on the SE group only with the RAM to match. Large objects are limited by the network before
the CPU: the lab moved 1.1 Gbit/s through a 2 vCPU engine from one generator.

### Controller nodes (three, each running etcd, PostgreSQL, API, worker and proxy)

Measured: the whole stack of one node used about 400 MB of RAM and a few percent of a vCPU idle; the database was 33 MB. **The API is one process
per node and CPU-bound**: on 2 vCPU a node served about 10 authenticated reads, 13 writes (change sets) and 165 health checks per second, and a
power-off of any one node or of the database leader was survived (the controller answered again after 25 to 30 s). The data-plane traffic never touches the
controller. So size by how many people and how much automation use the API, and by how many objects exist, not by request rate on the engines.
More vCPU than M does not raise API throughput until the API runs several processes (not yet), so L and XL are listed for databases with
very many objects and not as a way to get more API capacity.

| Size | vCPU | RAM | Disk | Fits (estimated) | Status |
| --- | --- | --- | --- | --- | --- |
| XS | 2 | 4 GB | 20 GB | a lab or pilot: 2 tenants, about 10 engines, 100 virtual services, a few users | **measured** (the test cluster) |
| S | 2 | 4 GB | 40 GB | 5 tenants, 20 engines, 300 virtual services, ten or so users | estimated; the minimum for production |
| M | 4 | 8 GB | 60 GB | 20 tenants, 60 engines, 1 000 virtual services, tens of users | estimated |
| L | 8 | 16 GB | 100 GB | 100 tenants, 200 engines, 5 000 virtual services | estimated; the database and the worker are the users of the extra CPU |
| XL | 16 | 32 GB | 200 GB | more than that | estimated; watch the API's single process first |

Disk holds PostgreSQL (desired state, revisions, audit, jobs), etcd, Docker images (about 4 GB for the images of one release; keep two) and the
pgBackRest spool. Put etcd and PostgreSQL on storage that is not shared with bulk copies (a template clone on the same thin-provisioned volume stalled
etcd for about 10 s in the lab and caused a leader change). NTP must be synchronized on every node. Database connections: each API and worker process
keeps up to 16 per database role (`SWITCHYARD_DB_POOL_SIZE` and `_OVERFLOW`); `max_connections` is 200.

### Everything else

| Component | Size | Notes |
| --- | --- | --- |
| OpenBao | S: 2 vCPU, 2 GB, 10 GB, three nodes (raft) | small; a few requests per engine enrollment and per certificate. The lab ran one node |
| Backup repository (S3) | disk of 5 times the database size, at least 100 GB | four full backups plus WAL; configuration backups are tiny (1.5 KB in the lab) |
| Telemetry VM (Prometheus, Loki, Grafana) | S: 2 vCPU, 4 GB, 100 GB; M: 4 vCPU, 8 GB, 300 GB | **not measured**. Disk follows your request rate: 30 days of metrics and 14 days of logs by default (`PROM_RETENTION`, `loki.yaml`); watch the disk after the first week |
| Kubernetes (Helm) | the chart's requests are API 100m, worker 100m, proxy 20m; start each database instance of CloudNativePG at the controller node size above | the lab k3s ran 3 database instances, 2 API, 2 worker and 2 proxy pods on two small nodes |
| Proxmox hosts | enough for the engines (each group at least two, on different hosts) plus the three controller VMs placed as above | an engine roll needs room for one extra engine per group |

## 5. Security checklist

The security reviews are in [security/backend-review.md](security/backend-review.md) and
[security/ui-review.md](security/ui-review.md); ADRs [0072](adr/0072-hardening-engines-logins-secrets.md) and
[0074](adr/0074-agent-bundle-binding.md) hold the hardening decisions. Before go-live:

- [ ] No lab switches anywhere: `SWITCHYARD_INSECURE_AGENT`, `SWITCHYARD_ENGINE_POLICY=open` and `SWITCHYARD_SECRET_POLICY=open`
      are unset (`deploy/prod/node.env.example` never sets them).
- [ ] mTLS to engines is configured (`SWITCHYARD_DATAPLANE_CA`, `_CERT`, `_KEY`); the controller refuses to start with a
      bundle key and no CA.
- [ ] Engines run `sy-agent -controller-cn=<the controller client certificate's CN>` (`CONTROLLER_CN` in `enroll.env`), so
      only that certificate may call the agent.
- [ ] The OpenBao PKI role for agents has `use_csr_sans=false` and `use_csr_common_name=false`
      ([deploy/prod/README.md](../deploy/prod/README.md)); otherwise an agent can choose its own names.
- [ ] The controller's OpenBao policy is the output of `syctl openbao-policy`, with the shared prefixes you really use.
- [ ] Break-glass is off in normal operation (it is enabled for a reason and a time window); sign-in is through your identity
      provider; system tokens expire (30 days by default, 90 at most; [auth.md](auth.md)).
- [ ] The proxy in front of the UI is the one in `deploy/proxy/` (CSP, HSTS, `nosniff`, `frame-ancestors 'none'`, rate
      limits on login) and TLS ends at the management SE group or another trusted hop that sends `X-Forwarded-For`
      ([adr/0053](adr/0053-ui-proxy-and-headers.md)). The limit is per node.
- [ ] The Proxmox API token has only the rights it needs. The lab script grants broad built-in roles on `/`; for production
      create a role with just VM clone/config/power/allocate on the engine pool, datastore allocate, SDN allocate and audit,
      scope it to a resource pool, and review it.
- [ ] Networks: management is its own network, engines' data and VIP networks are separate, and nothing in the request path
      depends on the controller. There is no SSH from the controller to engines and none is needed.
- [ ] The database tier is hardened as in [Securing the database tier](#securing-the-database-tier): etcd and Patroni over
      TLS with a password on Patroni's REST API, `hostssl` for the three nodes only, and the host firewall loaded on every node.
- [ ] No secrets in PostgreSQL, logs or Git: only `secret_ref`s that point into OpenBao.

## 6. OpenBao

The quickstart runs a single OpenBao node with its unseal key on disk. For production:

- Run it on its own VMs with TLS, integrated (raft) storage on three nodes or another supported backend, and **auto-unseal**
  or a key ceremony with several key holders; never a sidecar that holds the key.
- KV v2 at `kv`. Layout the controller expects: `kv/tenants/<tenant>/...` (certificates, per tenant), `kv/se/...` (shared Data
  Plane credentials), `kv/break-glass/...` (the break-glass TOTP secret), plus the PKI mount used for agent certificates.
- The controller authenticates with AppRole and the policy from `syctl openbao-policy`
  (`--shared kv/se/` and one `--platform` per secret the controller itself reads: `kv/proxmox/token`, and the passphrase and
  credentials of each configuration backup target, for example `--platform kv/backup/passphrase --platform kv/backup/s3-access`).
  The policy includes the PKI signing path for agent certificates (`pki/sign/agents`); an older policy without it makes engine
  enrollment fail with HTTP 403. Rotate the AppRole secret id on a schedule.
- Back up OpenBao itself (raft snapshots) and test the restore. This repository does not yet script that.

## 7. Certificates and the CA

- Agent certificates are issued by the OpenBao PKI (7 days by default) and renewed by each engine every 8 hours
  ([adr/0011](adr/0011-agent-enrollment.md)). Rotating the CA is an overlap of two trusted CAs followed by a wait:
  [runbooks/rotate-ca.md](runbooks/rotate-ca.md) (the trust logic is rehearsed with `deploy/prod/ca-rotation-drill.sh`; the
  agent renewal part needs a real engine).
- Virtual-service certificates (Cloudflare Origin CA, ACME, imported) are stored in OpenBao and rolled to engines by the
  pipeline ([adr/0010](adr/0010-certificates-and-cloudflare.md), [adr/0043](adr/0043-pkcs12-certificate-import.md)).
- The controller's public certificate is served by the management SE group like any other service.

## 8. Backups and disaster recovery

- **Database:** pgBackRest to object storage, point-in-time restore, replicas rebuilt after a fresh backup:
  [runbooks/restore-database.md](runbooks/restore-database.md), rehearsed on the three-node lab (`deploy/prod/lab-drill.sh`).
  Take a backup before every upgrade.
- **Engines are disposable:** [runbooks/replace-se.md](runbooks/replace-se.md) and [runbooks/build-se-template.md](runbooks/build-se-template.md).
  Keep the template, the agent and Data Plane API binaries (or the means to rebuild them) and the enrollment procedure.
- **Configuration history:** every revision is in the database and mirrored to Git if you configure the mirror. A bad
  revision is undone with [runbooks/emergency-rollback.md](runbooks/emergency-rollback.md).
- **What you must also keep:** the bundle signing key, the PKI and the controller client certificate. Losing the bundle key
  means re-enrolling every engine.

## 9. VMs or containers for engines

Engines can be QEMU VMs (cloud-init template, the default) or privileged LXC containers with nesting. Containers are
lighter and need no KVM, but share the Proxmox host's kernel, and the host must load `ip_vs`, `ip_vs_rr`, `ip_vs_wrr`,
`ip_vs_sh` and `nf_conntrack` for UDP services ([adr/0055](adr/0055-guest-kind-vm-or-lxc.md)). Containers have no
cloud-init, so enrollment files are pushed in. Unprivileged containers: one VRRP engine enrolled, applied and served in the lab, but BGP/FRR, IPVS and failover between
them were not tried, so they are not supported yet. Deploying LXC guests is a system setting, `allow_lxc_guests`, that is **off by default**
(ADR 0076): a deploy with `guest_kind: lxc` while it is off answers `403` with code
`lxc_disabled`. A system administrator turns it on with `PATCH /api/v1/system/settings`, or `syctl setting allow_lxc_guests true`;
`SWITCHYARD_ALLOW_LXC=1` forces it on for a deployment (read-only in the API, as `allow_lxc_guests_forced`), which the lab and
the quickstart use. Enable it only where you accept the shared kernel.

### Proxmox privileges of the controller role

A role that deployed, enrolled and rolled QEMU engines in the lab (create it with `pveum role add SwitchyardController --privs "..."`,
give it to the API token on `/` or on the pool the engines live in; both the user and the token need it unless the token has
privilege separation off):

```text
Datastore.AllocateSpace Datastore.Audit Pool.Audit Sys.Audit
SDN.Allocate SDN.Audit SDN.Use
VM.Allocate VM.Audit VM.Clone VM.PowerMgmt
VM.Config.CPU VM.Config.Cloudinit VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options
VM.GuestAgent.Audit VM.GuestAgent.FileWrite VM.GuestAgent.Unrestricted
```

`VM.GuestAgent.Unrestricted` is needed for the `chmod` the controller runs in the guest after writing each enrollment file; without it
an engine roll or autoscale deploy waits in `awaiting_enrollment` and writes only the first file. Containers need the corresponding
container privileges as well; the lab used a broader role for those.

## 10. Monitoring

Prometheus, Loki and Grafana from `deploy/telemetry/` ([adr/0052](adr/0052-telemetry-pipeline.md),
[runbooks/telemetry.md](runbooks/telemetry.md)): scrape the controller (`/metrics`) and the per-engine HAProxy and node
exporters through the service-discovery endpoint, ship access logs with Vector, and use the provided dashboards and alert
rules (stale status poll, failed change sets, a down exporter). Grafana can be shown inside the UI, scoped per tenant by the controller ([adr/0085](adr/0085-grafana-behind-controller.md), [runbooks/telemetry.md](runbooks/telemetry.md)): keep Grafana reachable from the controller nodes only and its admin password and `GRAFANA_DS_SECRET` in your secret store. No Alertmanager is deployed; connect your own. Alert on
`switchyard_status_age_seconds` growing, failed apply jobs, certificates near expiry and engines with drift.

## 11. Upgrades

- With an upgrade bundle, `syctl upgrade apply` does the node steps below for a Compose node (verify, back up, load images, migrate,
  recreate, health check, roll back on failure); see [upgrades.md](upgrades.md) and the
  [lab run](lab/2026-10-13-upgrade-lab/index.md) for its prerequisites. Engines are rolled with the roll API.
- The `migrate` step runs `syctl db upgrade` and is idempotent; migrations are forward-only, so back up first.
- Upgrade the controller nodes one at a time (stop the node's worker, upgrade, check `GET /api/v1/healthz`). The proxy
  re-resolves the API address, so a recreated API container needs no proxy restart.
- Agents and controllers are upgraded together: bundles are bound to an engine and a rising sequence, and an agent refuses
  unbound bundles unless started with `-allow-unbound-bundles` ([adr/0074](adr/0074-agent-bundle-binding.md)).
- New HAProxy, FRR, Keepalived or agent versions reach engines by building a new template and replacing engines one at a time,
  health-gated: [runbooks/build-se-template.md](runbooks/build-se-template.md), [runbooks/rolling-upgrade.md](runbooks/rolling-upgrade.md)
  (the gate is `GET /tenants/{t}/status/se-groups`), [runbooks/replace-se.md](runbooks/replace-se.md).
- Every apply goes through the canary step; do not skip it.

## 12. What has and has not been proven

Proven in the Docker-hosted lab on a real Proxmox ([adr/0056](adr/0056-end-to-end-lab.md)): deploying engines through the
controller, enrollment and renewal, mTLS, a canary-first apply with per-engine bundles, VRRP failover and fail-back, a
controller-less reboot, a BGP session with BFD to an upstream, route announcement and withdrawal, and data-NIC addressing.
The three-node database restore and failover are rehearsed on a one-host lab, and on three VMs over two Proxmox hosts
([lab/2026-10-13-upgrade-lab](lab/2026-10-13-upgrade-lab/index.md)).

Proven on two real Proxmox hosts in a cluster ([runbooks/lab-hosts.md](runbooks/lab-hosts.md), section 7,
[adr/0079](adr/0079-two-host-lab-findings.md)): KVM VM engines with cloud-init on both hosts; VRRP failover between hosts in
4 to 5 seconds; UDP services through IPVS; an unprivileged container serving a VIP (VRRP only); ECMP over three BGP engines
spread over both hosts; the telemetry stack with real engines (Prometheus targets, all four dashboards, Loki logs); and the
whole set again with IPv6 only ([ipv6.md](ipv6.md)). Pilot items that remain: BGP or IPVS in unprivileged containers,
reboot recovery of every engine kind across hosts, and a real switch with VLAN trunks and IGMP snooping (the lab used a VXLAN
overlay).

Proven on the same hosts on 2026-10-13 ([prod deployment](lab/2026-10-13-prod-deploy/index.md),
[upgrades](lab/2026-10-13-upgrade-lab/index.md)): the three-node production Compose (hardened database tier, the controller VIP on
its own SE group, failover, backups to S3, restore from S3 into empty nodes) in LXC containers and in VMs; the Helm install with a
CloudNativePG failover; bundle upgrades of the nodes one at a time; configuration backups to S3 and SFTP and a restore into an
empty controller; a Helm upgrade with a schema migration; and an engine roll of QEMU engines. Not yet run: a roll of LXC engines
by the roll API, SAML and the other items listed in those pages.

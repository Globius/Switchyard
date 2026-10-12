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
- **Management tenant lock:** applying a change in `management` needs a system administrator and a reason, and the
  `controller` service and its SE group cannot be deleted or disabled ([adr/0051](adr/0051-management-tenant-lock-and-adopt.md)).
  Bring the first VMs in with `syctl adopt` (`deploy/prod/adopt/management.example.yaml`).
- **Two Proxmox hosts today:** put controller VMs 1 and 2 on different hosts and the third on another platform with its
  Patroni member tagged `nofailover`, as described in the backend design.
- **Engines:** HAProxy SEs cloned from the template in `se-image/`, placed by VRRP or BGP per SE group. SEs keep serving and
  failing over when the controller is down; tests in [adr/0056](adr/0056-end-to-end-lab.md) show a VIP surviving both the
  controller being stopped and the engines rebooting.
- **Monitoring** runs apart from the controller ([section 8](#8-monitoring)).

## 2. Sizing (starting points, not measurements)

Nothing here has been load-tested. Start with these and watch the dashboards.

| | |
| --- | --- |
| Controller VM | 2 to 4 vCPU, 4 to 8 GB RAM, 40 GB disk (PostgreSQL, etcd, Docker); NTP synchronized |
| Database | small: desired state, revisions, audit and the job queue. Backup repository sized for 4 full backups plus WAL |
| Engine | the deploy default is 2 vCPU and 2 GB RAM; HAProxy needs CPU for TLS and RAM for connections (`maxconn` on the SE group, default 20 000). Size by the busiest group and test it |
| Telemetry VM | 30 days of metrics and 14 days of logs by default; disk follows your request rate (`PROM_RETENTION`, `loki.yaml`) |
| Proxmox hosts | enough for engines (each group at least two, on different hosts) plus the three controller VMs placed as above |

## 3. Security checklist

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
- [ ] No secrets in PostgreSQL, logs or Git: only `secret_ref`s that point into OpenBao.

## 4. OpenBao

The quickstart runs a single OpenBao node with its unseal key on disk. For production:

- Run it on its own VMs with TLS, integrated (raft) storage on three nodes or another supported backend, and **auto-unseal**
  or a key ceremony with several key holders; never a sidecar that holds the key.
- KV v2 at `kv`. Layout the controller expects: `kv/tenants/<tenant>/...` (certificates, per tenant), `kv/se/...` (shared Data
  Plane credentials), `kv/break-glass/...` (the break-glass TOTP secret), plus the PKI mount used for agent certificates.
- The controller authenticates with AppRole and the policy from `syctl openbao-policy`
  (`--shared kv/se/ --platform kv/proxmox/token` as needed). Rotate the AppRole secret id on a schedule.
- Back up OpenBao itself (raft snapshots) and test the restore. This repository does not yet script that.

## 5. Certificates and the CA

- Agent certificates are issued by the OpenBao PKI (7 days by default) and renewed by each engine every 8 hours
  ([adr/0011](adr/0011-agent-enrollment.md)). Rotating the CA is an overlap of two trusted CAs followed by a wait:
  [runbooks/rotate-ca.md](runbooks/rotate-ca.md) (the trust logic is rehearsed with `deploy/prod/ca-rotation-drill.sh`; the
  agent renewal part needs a real engine).
- Virtual-service certificates (Cloudflare Origin CA, ACME, imported) are stored in OpenBao and rolled to engines by the
  pipeline ([adr/0010](adr/0010-certificates-and-cloudflare.md), [adr/0043](adr/0043-pkcs12-certificate-import.md)).
- The controller's public certificate is served by the management SE group like any other service.

## 6. Backups and disaster recovery

- **Database:** pgBackRest to object storage, point-in-time restore, replicas rebuilt after a fresh backup:
  [runbooks/restore-database.md](runbooks/restore-database.md), rehearsed on the three-node lab (`deploy/prod/lab-drill.sh`).
  Take a backup before every upgrade.
- **Engines are disposable:** [runbooks/replace-se.md](runbooks/replace-se.md) and [runbooks/build-se-template.md](runbooks/build-se-template.md).
  Keep the template, the agent and Data Plane API binaries (or the means to rebuild them) and the enrollment procedure.
- **Configuration history:** every revision is in the database and mirrored to Git if you configure the mirror. A bad
  revision is undone with [runbooks/emergency-rollback.md](runbooks/emergency-rollback.md).
- **What you must also keep:** the bundle signing key, the PKI and the controller client certificate. Losing the bundle key
  means re-enrolling every engine.

## 7. VMs or containers for engines

Engines can be QEMU VMs (cloud-init template, the default) or privileged LXC containers with nesting. Containers are
lighter and need no KVM, but share the Proxmox host's kernel, and the host must load `ip_vs`, `ip_vs_rr`, `ip_vs_wrr`,
`ip_vs_sh` and `nf_conntrack` for UDP services ([adr/0055](adr/0055-guest-kind-vm-or-lxc.md)). Containers have no
cloud-init, so enrollment files are pushed in. Unprivileged containers: one VRRP engine enrolled, applied and served in the lab, but BGP/FRR, IPVS and failover between
them were not tried, so they are not supported yet. Deploying LXC guests is a system setting, `allow_lxc_guests`, that is **off by default**
(ADR 0076): a deploy with `guest_kind: lxc` while it is off answers `403` with code
`lxc_disabled`. A system administrator turns it on with `PATCH /api/v1/system/settings`, or `syctl setting allow_lxc_guests true`;
`SWITCHYARD_ALLOW_LXC=1` forces it on for a deployment (read-only in the API, as `allow_lxc_guests_forced`), which the lab and
the quickstart use. Enable it only where you accept the shared kernel.

## 8. Monitoring

Prometheus, Loki and Grafana from `deploy/telemetry/` ([adr/0052](adr/0052-telemetry-pipeline.md),
[runbooks/telemetry.md](runbooks/telemetry.md)): scrape the controller (`/metrics`) and the per-engine HAProxy and node
exporters through the service-discovery endpoint, ship access logs with Vector, and use the provided dashboards and alert
rules (stale status poll, failed change sets, a down exporter). No Alertmanager is deployed; connect your own. Alert on
`switchyard_status_age_seconds` growing, failed apply jobs, certificates near expiry and engines with drift.

## 9. Upgrades

- The `migrate` step runs `syctl db upgrade` and is idempotent; migrations are forward-only, so back up first.
- Upgrade the controller nodes one at a time (stop the node's worker, upgrade, check `GET /api/v1/healthz`). The proxy
  re-resolves the API address, so a recreated API container needs no proxy restart.
- Agents and controllers are upgraded together: bundles are bound to an engine and a rising sequence, and an agent refuses
  unbound bundles unless started with `-allow-unbound-bundles` ([adr/0074](adr/0074-agent-bundle-binding.md)).
- New HAProxy, FRR, Keepalived or agent versions reach engines by building a new template and replacing engines one at a time,
  health-gated: [runbooks/build-se-template.md](runbooks/build-se-template.md), [runbooks/rolling-upgrade.md](runbooks/rolling-upgrade.md)
  (the gate is `GET /tenants/{t}/status/se-groups`), [runbooks/replace-se.md](runbooks/replace-se.md).
- Every apply goes through the canary step; do not skip it.

## 10. What has and has not been proven

Proven in the Docker-hosted lab on a real Proxmox ([adr/0056](adr/0056-end-to-end-lab.md)): deploying engines through the
controller, enrollment and renewal, mTLS, a canary-first apply with per-engine bundles, VRRP failover and fail-back, a
controller-less reboot, a BGP session with BFD to an upstream, route announcement and withdrawal, and data-NIC addressing.
The three-node database restore and failover are rehearsed on a one-host lab.

Proven on two real Proxmox hosts in a cluster ([runbooks/lab-hosts.md](runbooks/lab-hosts.md), section 7,
[adr/0079](adr/0079-two-host-lab-findings.md)): KVM VM engines with cloud-init on both hosts; VRRP failover between hosts in
4 to 5 seconds; UDP services through IPVS; an unprivileged container serving a VIP (VRRP only); ECMP over three BGP engines
spread over both hosts; the telemetry stack with real engines (Prometheus targets, all four dashboards, Loki logs); and the
whole set again with IPv6 only ([ipv6.md](ipv6.md)). Pilot items that remain: BGP or IPVS in unprivileged containers,
reboot recovery of every engine kind across hosts, a real switch with VLAN trunks and IGMP snooping (the lab used a VXLAN
overlay), IPv6 VIP withdrawal on a failed HAProxy (the renderer does not list IPv6 prefixes for the watchdog yet), and the
multi-node production Compose end to end across hosts.

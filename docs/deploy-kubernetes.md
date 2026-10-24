# Run the controller on Kubernetes

The Helm chart in `deploy/helm/switchyard` runs the controller stack on Kubernetes: the API, the job worker, the UI proxy and
(by default) a PostgreSQL cluster managed by CloudNativePG. It is the third supported shape next to Docker Compose
([quickstart.md](quickstart.md), [production.md](production.md)) and VMs or LXC containers on Proxmox. The decision is in
[adr/0191](adr/0191-kubernetes-deployment.md); what was run to prove it is in
[lab/2026-10-12-kubernetes-install.md](lab/2026-10-12-kubernetes-install.md).

The engines are unchanged: they are HAProxy VMs or containers on Proxmox (or other hosts) that keep serving when the
controller is down. Kubernetes only hosts the controller.

## What the chart creates

| Object | What it is |
| --- | --- |
| `<release>-api` Deployment, Service | `syctl serve`, 2 replicas, probes on `/api/v1/healthz`, spread over nodes |
| `<release>-worker` Deployment | `syctl worker`, 2 replicas, safe to run several (below) |
| `<release>-proxy` Deployment | the UI and `/api` behind the security headers of [adr/0053](adr/0053-ui-proxy-and-headers.md), plus an nginx TLS front |
| `<release>` Service, optional Ingress | the entry point: https on 8443 (and http on 8080 for an Ingress that ends TLS) |
| `<release>-migrate-<revision>` Job | `syctl db upgrade`, one per Helm revision; API and worker pods wait for it |
| `<release>-db` CloudNativePG `Cluster` | PostgreSQL 17, from `database.mode: cnpg` (or use your own database with `external`) |
| PodDisruptionBudgets | one pod of each Deployment may be down at a time |

The chart never creates a Secret and never holds key material; you give it the names of Secrets you made.

## What you need first

- Kubernetes 1.27 or later, a default StorageClass for the database, and a way in: a `LoadBalancer` Service, a NodePort, or an
  Ingress.
- The [CloudNativePG](https://cloudnative-pg.io) operator installed, unless you point the chart at your own PostgreSQL.
- An **OpenBao** the cluster can reach, set up as described in [production.md](production.md), section 6: the KV mount, the PKI
  mount with the `agents` role (`use_csr_sans=false`, `use_csr_common_name=false`), the controller policy from
  `syctl openbao-policy --shared kv/se/`, and an AppRole for it. It is not part of the chart.
- The two images, built from the repository root and pushed to a registry the cluster can pull from:

  ```sh
  docker build -f deploy/controller/Dockerfile -t registry.example.net/switchyard-controller:1.0 .
  docker build -f deploy/proxy/Dockerfile      -t registry.example.net/switchyard-proxy:1.0 .
  ```

- Time synchronized on the nodes, and network paths: engines must reach `publicUrl`, and the pods must reach the engines'
  management addresses (agent port 7443, Data Plane API 5556), the Proxmox API if you deploy engines from the controller, and
  OpenBao. The pods have the cluster's network, so an IPv4-only cluster cannot reach an IPv6-only Proxmox API directly.

## Create the Secrets

All in the release namespace. The files are the same ones the Compose installs mount at `/secrets`.

```sh
kubectl create namespace switchyard
openssl genpkey -algorithm ed25519 -out bundle.key    # signs the bundles engines accept; keep a copy, losing it means re-enrolling every engine
kubectl -n switchyard create secret generic switchyard-secrets \
  --from-file=bundle.key --from-file=ca.pem --from-file=client.crt --from-file=client.key \
  --from-literal=metrics-token="$(openssl rand -hex 16)"
kubectl -n switchyard create secret generic switchyard-openbao \
  --from-literal=role-id=... --from-literal=secret-id=...
kubectl -n switchyard create secret tls switchyard-tls --cert=server.crt --key=server.key
```

`ca.pem` is the CA that signs agent certificates, `client.crt` and `client.key` are the controller's client certificate (CN
`switchyard-controller`), and `switchyard-tls` is the certificate for the name browsers and engines use.

Use your secret manager (External Secrets, Sealed Secrets, Vault CSI) to create them if you have one, and turn on encryption at
rest for Secrets in the cluster: a Secret holds the bundle signing key. If you deploy engines from the controller, add the
Proxmox CA as key `pve-ca.pem` in `switchyard-secrets` and the API token secret as a Secret `switchyard-pve` (key `token-secret`).

## Install

```yaml
# values.yaml
publicUrl: https://switchyard.example.net            # what browsers and engines use; engines enroll against it
image:
  controller: {repository: registry.example.net/switchyard-controller, tag: "1.0"}
  proxy: {repository: registry.example.net/switchyard-proxy, tag: "1.0"}
secrets: {existingSecret: switchyard-secrets}
openbao: {addr: "https://openbao.example.net:8200", existingSecret: switchyard-openbao}
database:
  cnpg:
    instances: 3
    storage: {size: 50Gi}
tls: {enabled: true, secretName: switchyard-tls}
service: {type: LoadBalancer}
# controller.proxmox: {url: "https://pve.example.net:8006", tokenId: "switchyard@pve!controller", tokenSecret: {existingSecret: switchyard-pve}}
```

```sh
helm install sy deploy/helm/switchyard -n switchyard -f values.yaml
kubectl -n switchyard get pods -w
```

The database comes up first, the migration Job runs, and then the API and worker pods start (their init container waits for the
schema). Every value is documented in `deploy/helm/switchyard/values.yaml`. Other useful ones: `database.mode: external` with
`database.external.*` for a database you run, `ingress.*`, `hostAliases` (a name for a Proxmox host), `controller.extraEnv`
(any `SWITCHYARD_*` setting, [controller-settings.md](controller-settings.md)), `clusterDomain` (nginx needs the full service
name; default `cluster.local`), and `api.replicas`, `worker.replicas`, `proxy.replicas`.

With the Prometheus Operator installed, `monitoring.serviceMonitor.enabled` renders a `ServiceMonitor` that scrapes the
API Service's `/metrics` with the metrics token, and `monitoring.prometheusRule.enabled` renders a `PrometheusRule` with
the controller alerts of `deploy/telemetry/prometheus/rules.yml` (stale status poll, failed change sets, drifted engines).
Both are off by default; their labels must match your Prometheus's selectors, and an off-cluster Prometheus can use
`GET /api/v1/discovery/prometheus` instead ([operating.md](operating.md)).

## First sign-in and the first engine

Create an administrator token and the break-glass administrator as in [quickstart.md](quickstart.md), with the token made in the
cluster:

```sh
kubectl -n switchyard exec deploy/sy-api -- syctl token --name first
```

Everything after that (tenants, networks, SE groups, deploying and enrolling engines) is the same as on Compose. For
enrollment, run `deploy/quickstart/enroll-engine.sh` with `HOST_ADDR` and `HTTPS_PORT` set to the controller's public address
and `SECRETS_DIR` holding `ca.pem` (the CA in `switchyard-secrets`) and `dp_password` (the secret stored at `kv/se/dataplane`).

`syctl token` prints the token and a newline, so `TOKEN=$(syctl token)` captures it (`syctl token --json` prints
`{"name": ..., "token": ...}`). The token is `sy_` plus URL-safe base64, which includes `-` and `_`: quote it in shells.

Two different CAs are involved. The **agent CA** (`ca.pem`) signs the engines' client certificates. The agent also needs a CA
for the controller's **HTTPS** certificate. The script defaults to `ca.pem`, which is right only when the controller's HTTPS
certificate comes from that CA. When `publicUrl` has a publicly trusted certificate (for example Let's Encrypt), pass
`--controller-ca system` to use the engine's system CA bundle, or `--controller-ca FILE` with the CA that signed it.

Engines that enroll after the group's first apply do not receive the configuration by themselves: run
`POST /api/v1/tenants/{t}/se-groups/{group}/reapply` (canary first) once they are enrolled ([ADR 0214](adr/0214-pilot-deployment-fixes.md)).

### Scheduling the proxy

`proxy.nodeSelector`, `proxy.tolerations` and `proxy.affinity` work like those of `api` and `worker`. With
`service.externalTrafficPolicy: Local` and a LoadBalancer announced from some nodes only, set them so proxy pods run on those nodes.

### Exposure on a bare cluster, and a database failover

On a cluster without a cloud load balancer, k3s's built-in `servicelb` answers `type: LoadBalancer` with the node addresses (it is
on by default in k3s; it was off in the first lab run). The lab's second install used `service: {type: LoadBalancer}`
with `publicUrl: https://<node address>:8443`, which worked. With three CloudNativePG instances on two nodes, deleting the
primary's pod promoted another instance in about 30 seconds and the first database write through the API
succeeded about 35 seconds after the deletion ([lab/2026-10-13-prod-deploy](lab/2026-10-13-prod-deploy/index.md)).

## How it behaves with several replicas

- **API pods** hold no state; any pod answers any request.
- **Workers**: every worker claims jobs with `FOR UPDATE SKIP LOCKED`, so each job runs once. Periodic jobs (status poll,
  certificate renewal, housekeeping) are enqueued only by the worker that holds a PostgreSQL advisory lock; the lock is bound to
  its database connection, so when that pod dies another worker takes over within a few seconds.
- **Change pipeline**: applies stay serialized per tenant and go through the canary step as always.

## Upgrade

1. Back up the database first. Migrations only go forward. With CloudNativePG, take an on-demand backup (a `Backup` resource) or
   rely on a scheduled one (`database.cnpg.extraSpec.backup`, see the operator's documentation).
2. `helm upgrade sy deploy/helm/switchyard -n switchyard -f values.yaml` with the new image tags. A new migration Job runs, the
   pods wait for it, and the Deployments roll one pod at a time.
3. Check `kubectl -n switchyard rollout status deploy/sy-api` and the UI. Agents and controllers are upgraded together
   ([production.md](production.md), section 11).

Rolling back the chart does not roll back the schema: restore the database backup as well if a migration must be undone
([runbooks/restore-database.md](runbooks/restore-database.md) describes the Patroni install; for CloudNativePG use its
point-in-time recovery).

## Security notes

- Pods run as uid 10001 with a read-only root file system, no Linux capabilities and the `RuntimeDefault` seccomp profile.
- The controller uses the CloudNativePG superuser Secret (`<release>-db-superuser`); limit who can read Secrets in the
  namespace, and add NetworkPolicies so only the API, worker and migration pods reach the database and OpenBao.
- All the checklist items in [production.md](production.md), section 5 apply (no lab switches, mTLS to engines, break-glass off
  in normal operation).

## Not covered

An OpenBao chart, an Alertmanager and telemetry stack (see [runbooks/telemetry.md](runbooks/telemetry.md)), and publishing the
controller's own Service through Switchyard's management SE group (use a `LoadBalancer` or Ingress).

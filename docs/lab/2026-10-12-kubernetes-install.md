# Controller on Kubernetes and Compose in an LXC container (2026-10-12)

What was run to prove two of the three deployment shapes ([adr/0191](../adr/0191-kubernetes-deployment.md),
[deploy-kubernetes.md](../deploy-kubernetes.md)) on the two-host Proxmox lab ([runbooks/lab-hosts.md](../runbooks/lab-hosts.md)).

## A. Helm chart on k3s

**Cluster:** the two-node k3s v1.36 from [2026-10-12-kubernetes.md](2026-10-12-kubernetes.md) (one server on pve01, one agent
on pve02), CloudNativePG 1.27 for the database, a one-node OpenBao (`deploy/helm/bootstrap-test.sh`) with the PKI, policy and
AppRole, images imported into containerd. NodePort 30443 for the https entry point.

**Install:** `helm install sy deploy/helm/switchyard -n switchyard -f lab-values.yaml` with 2 API, 2 worker and 2 proxy
replicas, a single-instance database, the Proxmox API through `hostAliases`.

| Check | Result |
| --- | --- |
| Pods | database, migration Job, 2 × api, 2 × worker, 2 × proxy (api + TLS) all ready; `helm upgrade` three times (value changes) rolled one pod at a time, each with a new migration Job |
| Health through the Service | `GET /api/v1/healthz` over https on the node port: 200 |
| UI | `GET /` returns the UI with the security headers (CSP, HSTS) |
| Sign-in | break-glass administrator created with an API token, confirmed with TOTP, browser-style login `200`, `GET /me` `200` |
| Engine | an LXC engine deployed by the controller in the cluster through the Proxmox API (job in the worker), enrolled over `https://172.30.0.30:30443`; health score 100 |
| Virtual service | an HTTP service on a VIP applied through the change pipeline (canary first); `curl` from the helper returned 200 |
| Workers | with two workers exactly one PostgreSQL advisory lock is held (the scheduler leader); a worker pod was deleted and a replacement started with the lock still held |

**Found while doing it (all fixed in the chart or the lab, none needed a controller code change):**

- The pods could not read the mounted Secret files: add `fsGroup` to the pod security context.
- nginx in the proxy does not use search domains, so `sy-api:8000` did not resolve: the proxy now gets the service's full name
  (`clusterDomain`).
- The first install had no way to wait for the schema; the API and worker pods now have a `wait-schema` init container.
- The lab's k3s containers had a 1500-byte NIC on a 1430-byte overlay, so flannel (1450) black-holed large TLS records between
  nodes. The containers now set 1430 before k3s starts (a systemd drop-in) and flannel runs at 1380.
- The CloudNativePG operator and PostgreSQL images come from `ghcr.io`, which the lab hosts cannot reach (IPv4 only); they were
  pulled on the workstation and imported.
- The pods have only IPv4, the lab's Proxmox API is IPv6-only: it was reached through its IPv4 address on the overlay
  (`hostAliases`).

**Not proven:** a multi-instance database and its failover, `LoadBalancer` and Ingress on a real cluster, a pod restart while
OpenBao is down, upgrades that include a schema change.

## B. Docker Compose quickstart inside a privileged LXC container

A fresh container (Debian 13, **privileged**, features `nesting=1,keyctl=1`, 4 cores, 4 GB, 16 GB disk, address on the
management overlay), `docker.io` and `docker-compose` from Debian, then the quickstart exactly as documented:
`sh bootstrap.sh --host 172.30.0.6 --mtu 1400`.

| Check | Result |
| --- | --- |
| Docker in the container | overlay2, systemd cgroups, builds and runs the stack |
| Bootstrap | the stack came up (PostgreSQL, OpenBao with unseal sidecar, migrate, api, worker, proxy, TLS) and the first login material was created |
| Health and UI | `GET /api/v1/healthz` 200 over the TLS front; break-glass login 200; UI with security headers |
| Reboot of the container | all seven services came back by themselves; OpenBao was unsealed again and a new login worked |

Requirements this adds to the quickstart: a **privileged** container with `nesting=1` and `keyctl=1`; on an overlay with a
smaller MTU than the container's NIC, `--mtu` for the Docker network (`1400` here) and the NIC lowered the same way
(the lab quirk of [runbooks/lab-hosts.md](../runbooks/lab-hosts.md)).

**Found while doing it:**

- `bootstrap.sh` rebuilt the UI image on every run, which needs npm and Docker Hub; on a host without direct access it failed.
  It now reuses an existing image unless `--rebuild` (load the image with `docker load` where the build is not possible).
- A Docker client proxy configured for the build (`~/.docker/config.json`) is injected into the containers, and the API then
  sent its OpenBao calls to the proxy (500 on creating the break-glass administrator). Remove client proxy settings before
  starting the stack, or add the service names to `noProxy`.

Not run in the container: engines (the Proxmox API is not configured for this instance) and the telemetry stack.

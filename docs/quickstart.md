# Quickstart: a test instance on its own machine

This stands up a complete Switchyard on one Linux machine, connects it to a Proxmox cluster and deploys a first service
engine (SE). It is for evaluating and testing. It is **not** highly available and keeps some secrets where a production
install would not; [production.md](production.md) says what to change.

What you get: PostgreSQL 17, OpenBao (with a PKI for agent certificates), the controller (API and worker), the web UI
behind security headers, and a TLS front, all in Docker Compose (`deploy/quickstart/`). Monitoring is optional
([runbooks/telemetry.md](runbooks/telemetry.md)).

## 1. What you need

| | |
| --- | --- |
| The Switchyard machine | Linux (or any host with Docker and the Compose plugin), 2 vCPU, 4 GB RAM, 20 GB disk, `git`, `curl`, `openssl` |
| Network | The machine must be reachable by your browser, and **by the engines** on the HTTPS port (8443 by default). The engines enroll over https only. The controller must in turn reach the engines' management addresses (port 7443 for the agent, 5556 for the Data Plane API) |
| Proxmox | VE 9 with one or two nodes, prepared as in [runbooks/lab-hosts.md](runbooks/lab-hosts.md) (the `prep-host.sh` script checks a node and creates the API token) |

## 2. Start the stack

```sh
git clone <your GitLab URL for switchyard> && cd switchyard/deploy/quickstart
sh bootstrap.sh --host 192.0.2.10          # the address browsers and engines will use for this machine
```

The script is safe to run again. It builds the controller image, starts PostgreSQL and OpenBao, initializes OpenBao
(one key share, kept in `secrets/`), creates the PKI, the controller's policy and AppRole, a server and a client
certificate, the bundle signing key and the Data Plane credentials, starts everything, and prints:

- the URL (`https://<host>:8443/`), signed by the CA in `secrets/ca.pem` (import it in your browser, or use
  `curl --cacert secrets/ca.pem`);
- an admin API token;
- where the break-glass sign-in is (`secrets/breakglass.txt`: user, password, TOTP secret to add to an authenticator app).

Check it: `curl --cacert secrets/ca.pem https://<host>:8443/api/v1/healthz` answers `{"status":"ok"}`.

Use another HTTPS port with `--port 9443`. Everything it generates is in `.env` and `secrets/` (git-ignored): keep them, they
are the instance. `--rebuild` rebuilds the controller image after a `git pull` (otherwise an existing image is reused).

**IPv6 and several addresses.** `--host` takes a comma-separated list (`--host 192.0.2.10,2001:db8::10`): the first is what
browsers and engines are told to use, every one is a name in the certificate, and an IPv6 literal is bracketed in URLs for
you. `--ipv6` also publishes the front on the host's IPv6 addresses and gives the stack's Docker network IPv6 with NAT66
(`compose.ipv6.yaml`), which the API and worker need to reach engines that have only an IPv6 management address. Docker 26
needs IPv6 NAT switched on once (`/etc/docker/daemon.json`: `{"experimental": true, "ip6tables": true}`, then restart
Docker); newer Docker needs nothing. If the host forwards IPv6 (it does once Docker has IPv6), also set
`net.ipv6.conf.all.accept_ra=2`, or the host loses its default route. Add the engines' IPv6 range to `ENGINE_CIDRS` in
`.env`. The support matrix and limits are in [ipv6.md](ipv6.md).

## 3. Sign in

Open the URL, choose the break-glass sign-in, and use the user and password from `secrets/breakglass.txt` plus a TOTP code
from the authenticator app. That account is for first setup and emergencies. For daily use add an identity provider
(Admin, Identity providers; [auth.md](auth.md)) and turn break-glass off.

The API token works with `syctl`: `docker compose exec api syctl api login --url https://<host>:8443 --token <token>`
(inside the container), or install `syctl` from `controller/` with `uv`.

## 4. Prepare Proxmox

On each node, as root (details and the full checklist in [runbooks/lab-hosts.md](runbooks/lab-hosts.md)):

```sh
sh prep-host.sh --token          # on one node: also creates the API user and token (secret shown once)
```

Then build an engine template. It needs the two binaries, which `se-image/fetch-binaries.sh` produces with Docker
(`se-image/dist/`). Copy `se-image/` and the binaries to a node and run one of:

- **Container template** (works without KVM, lighter): `sh se-image/lxc/build-template.sh 9961 sy-agent dataplaneapi`
- **VM template**: `se-image/kvm/build-template.sh` (needs `/dev/kvm`)

Both finish with a real Proxmox *template* (the controller refuses to clone anything else). Note the template's VM id.
The bootstrap turns on `SWITCHYARD_ALLOW_LXC=1` (container engines are off by default in production; [production.md](production.md), section 7).

## 5. Connect the controller to Proxmox

```sh
sh connect-proxmox.sh --url https://pve1.example:8006 \
   --token-id switchyard@pve!controller --secret <the secret> --ca pve-root-ca.pem
```

`pve-root-ca.pem` is `/etc/pve/pve-root-ca.pem` from a node. The script stores the settings, restarts the API and worker and
prints the nodes it can see.

## 6. Describe your network and create the tenant

Edit `tenant.example.yaml` (a flat network that plays management, VIP and backend, one VRRP group, one pool), then:

```sh
sh adopt.sh tenant.yaml
```

You can build the same objects in the UI instead (Infrastructure: networks, address pools, routing profile, SE group).
For a real layout use separate management, VIP and backend networks and give each its own address pool; the deploy step
takes `nic_pools` to address the VIP and backend NICs ([adr/0057](adr/0057-data-nic-addressing.md)).

## 7. Deploy the first engine

In the UI: Infrastructure, SE groups, your group, Deploy engines. Or with the API:

```sh
curl --cacert secrets/ca.pem -H "Authorization: Bearer $(cat secrets/admin-token)" -H 'Content-Type: application/json' \
  -d '{"count":1,"nodes":["pve1"],"template_vmid":9961,"mgmt_pool_id":"<pool id>","credentials_ref":"kv/se/dataplane","guest_kind":"lxc"}' \
  https://<host>:8443/api/v1/tenants/lab/se-groups/<group id>/deploy
```

`credentials_ref` is the secret the bootstrap stored (`kv/se/dataplane`). The answer is a job id; watch it in the UI or at
`GET /tenants/lab/jobs/<id>`.

## 8. Enroll the engine

Cloning the template does not give the engine its enrollment token (Proxmox cannot upload cloud-init snippets through its
API, [adr/0011](adr/0011-agent-enrollment.md)). Create a token (UI: the engine's row, or
`POST /tenants/lab/engines/<id>/enrollment-token`), then:

```sh
sh enroll-engine.sh --kind lxc --node pve1 --vmid 100 --token <token>      # ssh to the node as root
```

The engine shows up as reachable within about a minute (UI: Infrastructure, engines; or `GET /tenants/lab/status/engines`).
The token is single use and expires after an hour. A VM answers "QEMU guest agent is not running" for its first seconds: run
the script again. When the controller is on another machine than the one you run the script on, give it a copy of the
controller's `secrets/ca.pem` and `dp_password` (`SECRETS_DIR=...`) and the address engines use (`HOST_ADDR=...`,
`HTTPS_PORT=...`). `--vector-env FILE` also hands the engine its log-shipping settings (the five variables of
[runbooks/telemetry.md](runbooks/telemetry.md)).

## 9. Publish a first service

UI: Virtual services, New. A minimal service is HTTP on port 80, a VIP from the VIP pool and a pool whose member is a web
server you run. Review the change set, apply. The canary engine takes the change first; then the VIP answers:
`curl http://<vip>/`.

## 10. Add monitoring (optional)

Prometheus, Loki and Grafana beside this stack (set `PROMETHEUS_URL=http://prometheus:9090` and `LOKI_URL=http://loki:3100` in the quickstart `.env` and run `docker compose up -d api worker` there, so the UI charts and log tail find them): in `deploy/telemetry/` copy `telemetry.env.example` to `telemetry.env` with
`CONTROLLER_SCHEME=http` and `CONTROLLER_HOSTPORT=api:8000`, write the `METRICS_TOKEN` of the quickstart's `.env` into
`secrets/metrics.token` (readable by everyone: the Prometheus container runs as `nobody`), and start it with
`docker compose -f compose.yaml -f compose.quickstart.yaml --env-file telemetry.env up -d`. The override puts Prometheus on
the quickstart's Docker network so it scrapes the API without the TLS front. The Proxmox dashboard
(`--profile pve`) works with the same API token (it has the auditor role); see [runbooks/telemetry.md](runbooks/telemetry.md).

## 11. Day two

| | |
| --- | --- |
| Stop / start | `docker compose --env-file .env down` / `up -d` (OpenBao is unsealed again by a helper container) |
| Logs | `docker compose logs -f api worker` |
| Start over | `docker compose down -v && rm -rf .env secrets` (this deletes the database and all keys) |
| Upgrade | `git pull`, `sh bootstrap.sh --rebuild` (migrations run first) |

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `bootstrap.sh`: "OpenBao stays sealed" | `secrets/unseal.key` is missing or wrong; restore it or start over |
| 502 from the front after an upgrade | restart the proxy: `docker compose restart proxy tls` |
| Browser warns about the certificate | import `secrets/ca.pem`; the certificate names are the `--host` you gave |
| Engine "unreachable", error mentions the certificate | the agent's certificate needs `use_csr_sans=false` on the OpenBao role `agents` (the bootstrap sets it) and the controller must reach the engine's management address |
| Deploy fails with "Pool overlaps" or address errors | the pools must lie inside the network, and engine addresses and VIPs must use separate ranges |
| Proxmox login fails | check the token id (`user@realm!name`), that the node name in `nodes` matches Proxmox, and `--ca` |

The deeper lab with two engines, failover and BGP is described in [runbooks/lab-hosts.md](runbooks/lab-hosts.md) and
[adr/0056](adr/0056-end-to-end-lab.md).

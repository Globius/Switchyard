# SE image

Recipe for the Proxmox VM template that SE group deploys clone (`template_vmid`).

| File | Purpose |
| --- | --- |
| `packages.txt` | Debian 13 packages (FRR, Keepalived 2.3, ipvsadm, nftables, node exporter, ...) |
| `provision.sh` | Idempotent installer: packages, HAProxy 3.2 (haproxy.debian.net), Data Plane API, `sy-agent`, units, sysctl |
| `files/` | systemd units, sysctl, kernel modules, Data Plane API config, bootstrap `haproxy.cfg` |
| `cloud-init/template-build.yaml` | User-data for the one-off build VM |
| `files/sy-vector-sinks` | Writes the Loki and/or OTLP Vector sinks from `/etc/switchyard/vector.env` at start (ADR 0221) |
| `cloud-init/se-instance.yaml.tpl` | Per-SE user-data: controller URL, one-time enrollment token, controller CA |
| `tofu/` | OpenTofu (bpg/proxmox) for the build VM |
| `test/` | Docker test: provisions Debian 13, checks versions and units, runs Data Plane API over mTLS |

Ports on a provisioned SE: agent 7443 (mTLS), Data Plane API 5556 (mTLS), node exporter 9100,
HAProxy exporter 8405 (a rendered listener, not part of the bootstrap config), local health 127.0.0.1:8404.

Container variant: `lxc/build-template.sh VMID AGENT DATAPLANE` (run on a Proxmox node) builds the same image as a privileged
LXC template, for deploy requests with `guest_kind: "lxc"` (ADR 0055). Load the IPVS and conntrack modules on the host.

The agent refuses bundles that are not bound to its engine name and a rising sequence (ADR 0074); the sequence is kept in
`/etc/switchyard/agent/bundle-seq`, inside the agent's writable `/etc/switchyard`. `CONTROLLER_CN` in `enroll.env` is the
common name of the controller's client certificate; the unit passes it as `-controller-cn`, so only that certificate may call the
agent. Leave it empty only in throwaway labs (the agent then logs a warning and accepts any certificate the CA signed).

Enrollment is https only (the agent refuses an `http://` controller URL). The agent also writes `certs/*.crl`
(client CA revocation lists) beside the certificates in `/etc/switchyard/certs`; that directory is inside the
agent's `ReadWritePaths` and is the Data Plane API's `ssl_certs_dir`, so no extra path is needed.

Boot behaviour: HAProxy, Keepalived and FRR are all enabled. Keepalived and FRR skip themselves until the controller has written their
config under `/etc/switchyard/`, and the agent starts them when a config appears (and stops them when it goes). An SE that reboots
therefore comes back with its VIP and BGP sessions with the controller down. `frr-pythontools` is installed because
`systemctl reload frr` needs it.

Test: `docker build -f se-image/test/Dockerfile -t sy-se-image-test .` from the repository root.
Procedure: `docs/runbooks/build-se-template.md`. Decision: ADR 0050.

## WAF sidecar (Coraza SPOA)

`files/coraza-spoa.service` runs `coraza-spoa` on `127.0.0.1:9000` from `/etc/switchyard/coraza/config.yaml`, which the agent writes when a
service has a `waf` policy (ADR 0140 to 0144). The image build still has to install the `coraza-spoa` binary at `/usr/local/bin/`, the OWASP CRS
release named in `CRS_VERSIONS` (`controller/switchyard/domain/models.py`) where Coraza resolves `@owasp_crs`, a `coraza` user, and enable the unit.
`files/vector.yaml` already ships its journal to Loki.

## CrowdSec sidecar (SPOA bouncer)

`files/crowdsec-spoa-bouncer.service` runs the CrowdSec HAProxy SPOA bouncer (MIT) on `127.0.0.1:9001` from
`/etc/switchyard/crowdsec/spoa-bouncer.yaml`, which the agent writes (and starts the unit for) when a service has a `crowdsec` policy
(ADR 0215). The file holds the bouncer key, so the directory is `root:crowdsec-spoa` `0750`. The bouncer pulls decisions from the
operator's CrowdSec local API over the network and keeps blocking from memory if that API is down. `fetch-binaries.sh` downloads the
release named in `versions.env` and checks its pinned SHA-256; `provision.sh` installs it when `CROWDSEC_SPOA_BIN` is given.

# SE image

Recipe for the Proxmox VM template that SE group deploys clone (`template_vmid`).

| File | Purpose |
| --- | --- |
| `packages.txt` | Debian 13 packages (FRR, Keepalived 2.3, ipvsadm, nftables, node exporter, ...) |
| `provision.sh` | Idempotent installer: packages, HAProxy 3.2 (haproxy.debian.net), Data Plane API, `sy-agent`, units, sysctl |
| `files/` | systemd units, sysctl, kernel modules, Data Plane API config, bootstrap `haproxy.cfg` |
| `cloud-init/template-build.yaml` | User-data for the one-off build VM |
| `cloud-init/se-instance.yaml.tpl` | Per-SE user-data: controller URL, one-time enrollment token, controller CA |
| `tofu/` | OpenTofu (bpg/proxmox) for the build VM |
| `test/` | Docker test: provisions Debian 13, checks versions and units, runs Data Plane API over mTLS |

Ports on a provisioned SE: agent 7443 (mTLS), Data Plane API 5555 (mTLS), node exporter 9100,
HAProxy exporter 8405 (a rendered listener, not part of the bootstrap config), local health 127.0.0.1:8404.

Test: `docker build -f se-image/test/Dockerfile -t sy-se-image-test .` from the repository root.
Procedure: `docs/runbooks/build-se-template.md`. Decision: ADR 0050.

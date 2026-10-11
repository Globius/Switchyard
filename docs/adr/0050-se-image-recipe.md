# 0050. SE image recipe

Status: Accepted
Date: 2026-10-11

## Context
06 lists `se-image/` as not started; deploy clones a template by `template_vmid`. 01/02 call for a Debian 13
image with HAProxy 3.2 and Data Plane API, FRR, Keepalived 2.3, IPVS, node exporter and the agent,
built with cloud-init and OpenTofu.

## Decision
- One idempotent `provision.sh` is the single source of truth. It runs unchanged in cloud-init on the Proxmox
  build VM and in a Debian 13 container in tests, so the test exercises the real recipe.
- HAProxy 3.2 comes from haproxy.debian.net (Debian 13 ships 3.0). The Data Plane API binary and `sy-agent` are
  supplied by the caller (pinned release, built agent) rather than downloaded inside the script.
- HAProxy runs from `/etc/switchyard/haproxy/haproxy.cfg` with a bootstrap config; the Data Plane API
  (mTLS with the agent's certificate, port 5555) replaces it, as the adapter's `CONFIG_PATH` expects.
- Enrollment: the per-SE cloud-init snippet writes the controller URL, CA and a one-time token;
  `sy-agent-enroll.service` trades it for a certificate (ADR 0011) and deletes the token; a timer renews every 8 h.
- Machine identity (machine-id, cloud-init state) is cleared at the end so clones are unique.
- Metrics: node exporter on 9100 is in the image. The HAProxy exporter on 8405 must be a listener in the
  rendered configuration (renderer change, not done here).

## Consequences
Template builds are repeatable and tested without Proxmox. The OpenTofu module is validated (`tofu validate`)
but not applied; a real Proxmox build is covered by the lab test. The bpg download-file resource is deprecated
upstream and will need renaming.

## Alternatives considered
Packer: another tool and licence concern (HashiCorp BSL). virt-customize: not cloud-init, harder to test in Docker.

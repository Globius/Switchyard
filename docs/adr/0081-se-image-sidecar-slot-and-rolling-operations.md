# ADR 0081: SE image sidecar slot, pinned versions, rolling upgrade and health score

Status: Accepted

## Context

Block P7 (Operate, [design/09](../design/09-parity-work-blocks.md)) finishes what the operations items of
[design/06](../design/06-planned-vs-built.md) left: a rolling SE upgrade and scale-out procedure, a health score, live
checks of features that only had mocked tests, and the SE image changes later blocks depend on. P8 adds an auth sidecar
(oauth2-proxy first) and P10 a WAF sidecar (Coraza SPOA); both need the image to carry a daemon that runs only on engines
that use the feature, receives its configuration and secrets through the signed bundle, and is built from pinned,
checked binaries.

## Decision

- **A sidecar slot in the image.** `sy-auth.service` runs `/usr/local/bin/oauth2-proxy` as the unprivileged user `sy-auth`
  under a strict sandbox (no capabilities, read-only filesystem, no new privileges, a system-call filter, memory cap). It
  is enabled but has `ConditionPathExists=/etc/switchyard/auth/oauth2-proxy.cfg`, so an engine without an auth
  configuration carries no idle daemon, and one with it comes back after a reboot without the controller. The service gets
  its configuration through `LoadCredential=` (the agent writes the files owner-only for root) and its secrets as
  environment variables from `/etc/switchyard/auth/secrets.env`, never on a command line. The listening port is not part of
  this ADR: the auth contract with HAProxy (P8) fixes it.
- **The agent runs the slot.** `auth/` is a managed directory (unit `sy-auth`, files owner-only) and the unit is
  *on demand* like Keepalived and FRR: started when the first file appears, restarted on change (oauth2-proxy cannot
  reload), stopped when the last file goes. This generalizes the former Keepalived and FRR special case into one set
  (`onDemand`). The Coraza slot of P10 joins the same set.
- **Pinned, checked binaries.** `se-image/versions.env` holds the HAProxy series, the Go and Data Plane API images and the
  oauth2-proxy release with its SHA-256. `fetch-binaries.sh` downloads the release, verifies the pinned checksum and
  records the binary's own checksum next to it; `provision.sh` verifies that file before installing. The image test
  (`se-image/test/`) provisions with the sidecar and asserts the slot and the regressions of ADR 0079.
- **Health score.** `GET /tenants/{t}/status/se-groups` returns a 0 to 100 score for each SE group and engine, with the
  reasons ([services/health.py](../../controller/switchyard/services/health.py)): unreachable is 0; drift, pool members
  down, an unhealthy HAProxy, withdrawn routes, BGP sessions not established and a VRRP fault subtract fixed amounts; a group
  is the mean of its engines, capped at 50 without two healthy engines. It is a pure function of the stored status,
  deterministic and cheap, and the number a rolling upgrade gates on.
- **Rolling upgrade and scale-out are an operator procedure over the API, not a controller job.** An engine cannot
  enroll without secrets delivered into the guest, and Proxmox's API cannot do that (ADR 0011), so a controller-run job
  would stop at the same manual step as today. `deploy/lab/hosts/upgrade.sh` does the loop with the helpers the lab uses
  (deploy one replacement per engine on the same node, enroll, `reapply` the group so the new engine gets the current
  configuration, wait until the group scores 80 or more, retire the old engine); `--add` is scale-out. It stops at the
  first failure with both engines in place. Automatic scale-out stays deferred.
- **Live checks.** `deploy/lab/checks/live.sh` runs the PKCS#12 import (with a password, without, with a wrong one, and
  replace) and the 412/428 conflict responses against a real stack with OpenBao. Both pass, closing the two items 06 listed
  as never run live.

## Consequences

- P8 and P10 add a controller renderer for `auth/` (and `coraza/`) bundle files and the HAProxy side; the image, agent
  allowlist, secrets handling and checksum chain are already there.
- Templates must be rebuilt to carry the slot ([runbooks/build-se-template.md](../runbooks/build-se-template.md)). Roll the
  image and agent before a controller that sends `auth/` files.
- The score's weights are a first estimate, kept in one module so they can change with experience.

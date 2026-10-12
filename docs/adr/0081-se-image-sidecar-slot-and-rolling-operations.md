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

- **Sidecar slots in the image.** Two sidecars have a slot, and both follow one contract:
  - *Auth (P8).* `sy-auth.service` runs `/usr/local/sbin/sy-auth-run` as the unprivileged user `sy-auth`, which starts one
    oauth2-proxy per `/etc/switchyard/auth/<name>.cfg`, with `<name>.env` loaded into that instance's environment when it
    exists (secrets never on a command line). If one instance exits, all restart. Each configuration sets its own
    listener (loopback only; the P8 ADR allocates ports).
  - *WAF (P10).* `coraza-spoa.service` (user `coraza`) runs `coraza-spoa` on `127.0.0.1:9000` from
    `/etc/switchyard/coraza/config.yaml` and reloads on SIGHUP. The pinned OWASP Core Rule Set is unpacked at
    `/usr/share/coreruleset/<version>` (`rules/`, `crs-setup.conf.example`) with a `current` link.
  - *Contract.* The agent writes the bundle files of `auth/` and `coraza/` as root, group = the sidecar's group, mode 0640, so
    the sidecar reads them and nothing else on the engine does. The unit is enabled but conditional on its configuration
    (`ConditionPathExists[Glob]`), so an engine without the feature carries no idle daemon and one with it comes back after a
    reboot without the controller. Units are sandboxed (no capabilities, read-only filesystem, no new privileges).
    HAProxy's side (the calls, the failure mode) is each block's ADR; the image guarantees the processes and paths.
- **The agent runs the slots.** `auth/` and `coraza/` are managed directories (units `sy-auth` and `coraza-spoa`) and both units
  are *on demand* like Keepalived and FRR: started when the first file appears, restarted (auth: oauth2-proxy cannot reload)
  or reloaded (coraza) on change, stopped when the last file goes. This generalizes the Keepalived and FRR special case
  into one set (`onDemand`).
- **Pinned, checked binaries.** `se-image/versions.env` holds the HAProxy series, the Go and Data Plane API images, the
  oauth2-proxy release with its SHA-256, the coraza-spoa tag (no binary releases exist, so `fetch-binaries.sh` builds it with
  the Go image, verified by the module checksum database) and the Core Rule Set tarball with its SHA-256. `fetch-binaries.sh`
  verifies what it downloads and records the binaries' checksums beside them; `provision.sh` verifies again before
  installing. The image test (`se-image/test/`) provisions with all sidecars and asserts the slots and the regressions of
  ADR 0079.
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

- P8 and P10 add a controller renderer for `auth/` and `coraza/` bundle files and the HAProxy side; the image, agent
  allowlist, file ownership and checksum chain are already there.
- Templates must be rebuilt to carry the slot ([runbooks/build-se-template.md](../runbooks/build-se-template.md)). Roll the
  image and agent before a controller that sends `auth/` files.
- The score's weights are a first estimate, kept in one module so they can change with experience.

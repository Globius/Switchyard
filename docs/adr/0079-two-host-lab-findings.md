# ADR 0079: What the two real Proxmox hosts changed

Status: Accepted

## Context

[ADR 0056](0056-end-to-end-lab.md) proved the engine contract in a Docker-hosted lab on one Proxmox. The first two real
hosts (a cluster, IPv6-only, no internet on the lab networks) ran the scenarios the Docker lab could not: KVM engines on
two hosts, VRRP and BGP across hosts, UDP through IPVS, an unprivileged container, telemetry with real engines, and the
same again with IPv4 removed ([ipv6.md](../ipv6.md)). Everything below was found by running it, and each item has a test
or a script check.

## Decision

Fixes in the SE image, the agent, the Proxmox adapter and the lab and quickstart scripts:

- **VM clones do not upgrade packages at first boot.** Proxmox's generated cloud-init data runs a package upgrade, which
  fails (and marks cloud-init as errored) where the engine network has no internet. The adapter sets `ciupgrade=0` on
  every VM it clones. An engine must not need the internet to start.
- **nftables works on an engine.** The image never started the `nftables` unit, so the UDP masquerade rules (ADR 0055)
  could not load and the first UDP apply failed. The image now ships `/etc/nftables.conf` (it replaces and includes
  `/etc/switchyard/nftables/*.nft`) and enables the unit; the agent restarts the unit instead of reloading it, because a
  one-shot unit that has not run cannot reload.
- **Vector runs.** The packaged unit validated `/etc/vector/vector.yaml` (absent), so shipping logs never worked on a real
  engine. The drop-in now clears `ExecStartPre` and validates the controller-owned file. The image does not get
  `vector.env` through cloud-init for engines cloned through the API (snippets cannot be uploaded, ADR 0011), so
  `enroll-engine.sh` writes it with `--vector-env`.
- **IPv6 services work.** The image sets `net.ipv6.ip_nonlocal_bind` (HAProxy binds an IPv6 VIP before VRRP has it),
  `net.ipv6.conf.all.forwarding` (IPVS NAT of IPv6 UDP drops packets without it) and `accept_ra = 2` (forwarding switches
  router advertisements off otherwise, which removes the default route).
- **The agent understands IPv6 BGP.** `status.ParseBGP` read only the IPv4 address family, so an IPv6-only engine reported
  no BGP session and the canary failed. The route-health watchdog always withdrew and re-announced in the IPv4 family;
  it now picks the family from the prefix.
- **The agent stops on SIGTERM within seconds.** A graceful gRPC stop waits for the controller's `Watch` stream, so every
  restart, reboot and `pct stop` waited 90 seconds for systemd to kill it. The graceful stop now gives in-flight calls
  five seconds, then closes everything.
- **The Proxmox adapter writes IPv6 addresses** with `ip6=` and `gw6=` (VM `ipconfigN`, container `netN`), and the deploy
  job brackets IPv6 literals in the engine URLs.
- **The quickstart works over IPv6 and from a guest.** `bootstrap.sh --host A,B --ipv6` names every address in the TLS
  certificate (it treated an IPv6 literal as a DNS name), publishes the front on `[::]` and gives the Docker network IPv6
  with NAT66 (`compose.ipv6.yaml`), so the API can reach engines that have only IPv6. Image builds use the host network
  (a default Docker bridge has no IPv6 egress) and uv comes from PyPI (ghcr.io has no IPv6). `--rebuild` rebuilds the
  controller image. The nginx front also listens on `[::]`.
- **Lab scripts run against a controller that is not on the operator's machine** (`CONTROLLER_EXEC`, `api.sh`,
  `SECRETS_DIR`, `SSH_OPTS`), against the cluster's IPv6 addresses, and in a second tenant (`LAB_TENANT`, `tenant6.yaml`).

Findings reported, not fixed here: the BGP renderer writes only IPv4 prefixes into `frr/vips.json`, so the watchdog never
withdraws an IPv6 VIP (Backend; the agent side is fixed and was checked by hand-writing the file).

## Consequences

- A lab with lower MTU than the guests (a VXLAN overlay over a 1500 underlay) breaks gRPC between the controller and a
  container: Proxmox applies a container's `mtu=` only to the host end of the veth. The lab lowers the container MTU
  after deploy (`LAB_MTU`); real networks should carry 1500 or hand out the MTU (RA, DHCP).
- Templates built before this change keep the old behavior (no nftables, no IPv6 sysctls, old agent). Rebuild them
  ([runbooks/build-se-template.md](../runbooks/build-se-template.md)).
- Still not proven: the production Compose end to end across the two hosts, and reboot recovery of every engine kind in
  the two-host lab.

# Set up two Proxmox hosts for the Switchyard lab

What the hosts are for: the lab items the Docker-hosted lab cannot show ([adr/0056](../adr/0056-end-to-end-lab.md)) —
engines as real KVM VMs with cloud-init, UDP services through IPVS, unprivileged containers, ECMP across three or more BGP
engines, engines spread over two hosts (VRRP and BGP across them) and the production Compose end to end. Follow the list top
to bottom; each step says how to check it.

## 1. The two hosts

| | Minimum |
| --- | --- |
| Count | 2 (a single host covers all but the cross-host items) |
| CPU / RAM / disk | 4 cores, 16 GB, 60 GB each; hardware virtualization on (VT-x / AMD-V) in the BIOS |
| Software | Proxmox VE 9, same version on both, fully updated, clock synchronized (NTP) |
| Network | one NIC each is enough; a switch that carries VLANs between them (below) |

Install Proxmox VE 9 on both with static management addresses and different hostnames (for example `pve1`, `pve2`).

## 2. Cluster them (optional but recommended)

```sh
pvecm create lab                # on pve1
pvecm add <pve1 address>        # on pve2
pvecm status                    # both nodes, "Quorate: Yes"
```

A two-node cluster has no quorum if one node is down. That is fine for this lab (only guests are stopped, not hosts).
Without a cluster the lab can still drive each host, but not deploy across both in one job.

## 3. Network

All three lab networks (management, VIP, backend) have to exist on both hosts and carry client traffic, VRRP and BGP
between them. Two ways to get that:

**A switch that trunks VLANs.** Use one **VLAN-aware bridge** (`vmbr0`) on each host (tick "VLAN aware") and trunk the VLANs
between the hosts on the switch.

| Network | Used for | Suggested VLAN | Subnet |
| --- | --- | --- | --- |
| management | controller to engines, enrollment | 101 | 172.30.0.0/24 |
| VIP | client traffic to virtual IPs | 102 | 10.99.0.0/24 |
| backend | engines to pool members | 103 | 10.98.0.0/24 |

**No switch support (cloud hosts, IPv6-only underlay).** Build an isolated overlay between the two hosts with
`deploy/lab/hosts/overlay.sh`, one VXLAN per network under a plain bridge (`labmgmt`, `labvip`, `labbe`), which the lab
then uses as its VNets. Run it on each node with the other's underlay address (IPv6 works):

```sh
sh overlay.sh --self 2001:db8::1 --peer 2001:db8::2 --addr labmgmt=172.30.0.1/24     # node 1: also the management gateway
sh overlay.sh --self 2001:db8::2 --peer 2001:db8::1                                   # node 2
```

Give the bridges the gateway addresses you want (add `--addr labmgmt=fd30:0:0:1::1/64` and so on for the IPv6 lab, and
set `net.ipv6.conf.all.forwarding=1` on that node). A VXLAN costs 70 bytes: the bridges get MTU 1430, VM NICs follow
that, container NICs do not (set `LAB_MTU=1430` in `hosts.env` and the lab scripts lower them after deploy). Proxmox's own
SDN VXLAN zone does not support an IPv6 underlay, which is why this script exists. Remove with `overlay.sh --remove`.

- VRRP (IP protocol 112) and multicast must pass between the hosts. On a switch with IGMP snooping, allow it or disable
  it for these VLANs. (A VXLAN carries it as is.)
- The machine that runs the controller (the quickstart stack) must be on the management network or routed to it, with an
  address in `CONTROLLER_ADDR`. Engines reach `https://<that address>:8443`. It may be a guest on the overlay (below).
- A router, a spare VM or one of the hosts on the management network holds the gateway address (`172.30.0.1`) the engines
  use as their default route.
- The lab networks need **no internet**: engines boot offline (ADR 0079). Building templates does need it, from the
  host's own network, not the lab networks. For operators who want internet from a lab guest anyway, `egress.sh` (on the
  node that holds the gateway addresses) sets up a proxy on the IPv4 gateway (`http_proxy=http://172.30.0.1:8888`; the
  node may have no IPv4 to NAT with) and NAT66 for the IPv6 lab network. NAT66 is not persistent.

## 4. Prepare each node

Copy `deploy/lab/hosts/prep-host.sh` to each node and run it as root:

```sh
sh prep-host.sh                                  # pve2
sh prep-host.sh --sdn "labmgmt:101 labvip:102 labbe:103" --token      # pve1 only: SDN VLAN zone `lab`, API user and token
```

It prints `OK`, `WARN` or `FAIL` for: the Proxmox version, `/dev/kvm`, the IPVS and conntrack modules (loaded now and at
boot), the bridge being VLAN-aware, storage content types (snippets, container templates), the Debian container template,
outbound access, NTP, and, with the flags, the SDN zone, the API user and the token. The token secret is printed **once**;
save it. Fix every `FAIL` and read each `WARN`.

The token is the controller's identity. The script gives the user built-in roles on `/` (VM, datastore, SDN, pool admin and
auditor) for convenience; production uses a narrower role ([production.md](../production.md), section 3).

## 5. Hand over the access details

Fill in `deploy/lab/hosts/hosts.env.example` (copy it to `hosts.env`; it is git-ignored, never commit it) and share it through
the project thread, or the equivalent values:

- API URL (an IPv6 address in brackets: `https://[2001:db8::1]:8006`), token id and secret, the Proxmox root CA
  (`/etc/pve/pve-root-ca.pem` from a node);
- node names, and key-based root ssh to each node from the machine that runs the lab (the templates are built over ssh;
  `SSH_<node>=root@2001:db8::1` needs no brackets, `SSH_OPTS="-i KEY"` adds options);
- the three VNet or bridge names and the subnets above, the controller machine's address, and the template base id (default 9960);
- when the controller is a guest and not this machine: `CONTROLLER_EXEC` (how to run `api.sh` there, for example
  `ssh -i KEY root@node pct exec 9980 -- /usr/local/bin/sy-api`) and `LAB_MTU` for an overlay (above).

Check it: `sh deploy/lab/hosts/preflight.sh` prints `OK` / `WARN` / `FAIL` for the API login, both nodes online, quorum, the
VNets, the templates (once built, they must be real Proxmox templates) and ssh, IPVS and KVM on every node.

## 6. What gets run

`sh deploy/lab/hosts/run.sh` does everything after the preflight: builds the engine binaries, starts the controller on this
machine (the quickstart stack, `deploy/quickstart/`), connects it to Proxmox, builds the templates on **each** node (container,
unprivileged container and VM; ids `TEMPLATE_BASE` + 10 per node + 1, 2, 3, so the first node has 9961/9962/9963 and the second
9971/9972/9973), creates the lab tenant (`deploy/lab/hosts/tenant.yaml`) and a helper guest (client, web and UDP backend,
BGP upstream), then runs `scenario.sh`:

| Scenario | Proves |
| --- | --- |
| VM engines, one per host | deploy from each node's own template, enrollment through the guest agent, canary-first apply, the VIP answers, VRRP failover **between hosts** |
| UDP through IPVS | a UDP virtual service (Keepalived virtual server) forwards to a UDP backend and the echo comes back (skipped when `ip_vs` is missing) |
| Unprivileged container | a trial that reports what works; it never fails the run |
| BGP with ECMP | three engines over both hosts peer with the helper; the upstream shows three equal-cost paths, two when one engine's HAProxy stops, three again after |

Templates live on local storage, so each node builds its own and the scenario deploys to a node with that node's template
(one deploy request per node). With shared storage one template would do.

`run.sh` assumes the controller runs on the machine that runs it. When the controller is a guest on the overlay (the case
below), do the steps by hand in this order, using `lib.sh` (it reads `hosts.env`): the quickstart in the guest
(`bootstrap.sh --host ADDRESS`), `connect-proxmox.sh` with `--resolve NAME=ADDR` when the node name is not resolvable
from the guest, the templates, `adopt.sh tenant.yaml`, `helper.sh` on the first node, then `scenario.sh`.

## 7. What was run, and what it showed

Two Proxmox VE 9 nodes in a cluster, IPv6-only, no switch control, so the overlay above. The controller was the quickstart
in a guest on the first node, connected to the cluster's real API ([adr/0079](../adr/0079-two-host-lab-findings.md) lists
every fix this produced). On the IPv4 tenant:

| | Result |
| --- | --- |
| Two KVM engines, one per host, from each host's template | deployed through the API, enrolled through the guest agent, canary-first apply, VIP answers |
| VRRP failover between hosts | master VM stopped on one host, the VIP answered again from the other host after 4 s; failed back after restart |
| UDP through IPVS | echo through the VIP; needed the nftables fix |
| Unprivileged container | enrolled, applied, HAProxy and VRRP serve the VIP (one engine; BGP, IPVS and failover not tried) |
| BGP ECMP, three container engines (two on one host, one on the other) | three equal-cost paths at the upstream, two with one HAProxy stopped, three after; two with an engine on the other host stopped |
| Telemetry | Prometheus: controller, 6 HAProxy and 6 node targets up, plus the Proxmox exporter; 27 dashboard panels return data (the Proxmox guest panels too); Loki receives access logs from engines (after `enroll-engine.sh --vector-env`) |

And the IPv6-only twin (`tenant6.yaml`, `helper6.sh`, tenant `lab6`; ULA networks `fd30:0:0:1..3::/64`, no IPv4 in the
tenant, [ipv6.md](../ipv6.md)): two KVM engines enrolled over `https://[fd30:0:0:1::5]:8443` (one per host), VRRPv3
failover in 5 s, IPVS UDP over IPv6, three container engines peering with the upstream over IPv6 with three equal-cost
paths, and Prometheus scraping them at bracketed addresses. A stopped HAProxy does not withdraw its IPv6 VIP yet (the BGP
renderer's `vips.json` lists IPv4 prefixes only; reported to Backend).

Things this lab taught that are not in the scripts:

- Container NICs ignore the bridge MTU. Over a VXLAN (MTU 1430) gRPC from the controller to a container silently times
  out (`DEADLINE_EXCEEDED`, the TLS handshake completes with small packets only). Symptom: some engines reachable, some not.
- Turning IPv6 forwarding on (a NAT66 gateway, a Docker host with IPv6) switches router advertisements off; set
  `accept_ra=2` or the default route disappears.
- Proxmox reboots a container gracefully with `pct reboot`; before the agent stopped within seconds on SIGTERM it took 90 s.
- The guest agent is not ready for the first seconds of a VM boot: `enroll-engine.sh` fails with "QEMU guest agent is not
  running" until it is. Run it again.

## Public VIPs

`tenantpub.yaml` puts the VIP network on the hosts' own public /64 (the uplink bridge), with management and backend still
on the lab ULA networks, so a VIP is an internet-reachable IPv6 address while the backend stays lab-only (edit the prefix
first). Adopt it, deploy a VRRP group (the VIP NIC lands on `vmbr0`) and create an HTTP and a UDP service from the `vips`
pool. Checked from outside the hosts: `curl -g http://[VIP]/` returns the helper's answer through the VRRP engines, and a UDP
datagram to the UDP VIP's port 5301 is echoed back through IPVS. A BGP group would need a real upstream router for public
reachability, so BGP stays lab-only.

**HTTPS with a managed certificate.** Issue a server certificate from the lab CA (OpenBao `pki/issue/internal`, with the VIP as
an IP SAN), import it (`drive.py cert NAME`, the `certificates/import` API with the PEM in `CERT_PEM`) and create the service
with `drive.py vstls NAME GROUP VIP CERT`. Test from outside with the CA: `curl --cacert ca.pem https://[VIP]/` (on Windows
add `--ssl-no-revoke`).
The first TLS service used to fail validation because the certificate file reached the engine after the canary's check; the apply pipeline now sends certificates first (found by this lab, fixed in the pipeline).

## Rolling upgrade, scale-out and live checks

`deploy/lab/hosts/upgrade.sh` replaces every engine of a group one at a time (or adds engines with `--add`), gated on the group's
health score ([rolling-upgrade.md](rolling-upgrade.md), ADR 0083). `deploy/lab/checks/live.sh` runs the PKCS#12 import and the
412/428 conflict checks against the lab controller. Both use `hosts.env` and `lib.sh`; `LAB_TENANT` picks the tenant.

## Tearing down

Delete the lab guests (ids in `hosts.env` and the engines the controller created), remove the SDN zone `lab`
(`pvesh delete /cluster/sdn/zones/lab` after its VNets) or the overlay (`overlay.sh --remove`), and
`pveum user token remove switchyard@pve controller`.

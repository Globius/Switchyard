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

All three lab networks ride one **VLAN-aware bridge** (`vmbr0`) on each host: tick "VLAN aware" in Network, and trunk the
VLANs between the hosts on the switch.

| Network | Used for | Suggested VLAN | Subnet |
| --- | --- | --- | --- |
| management | controller to engines, enrollment | 101 | 172.30.0.0/24 |
| VIP | client traffic to virtual IPs | 102 | 10.99.0.0/24 |
| backend | engines to pool members | 103 | 10.98.0.0/24 |

- VRRP (IP protocol 112) and multicast must pass between the hosts. If the switch does IGMP snooping, allow it or disable
  it for these VLANs.
- The machine that runs the controller (the quickstart stack) must be on the management VLAN or routed to it, with an address
  in `CONTROLLER_ADDR`. Engines reach `https://<that address>:8443`.
- A router, a spare VM or a laptop on the management network needs to hold the gateway address (`172.30.0.1`) the engines use
  as their default route, with a route to the controller machine.
- Outbound internet (or an apt mirror) from the hosts, to build the engine templates.

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

Fill in `deploy/lab/hosts/hosts.env.example` (copy it to `hosts.env`, do not commit it) and share it through the project
thread, or the equivalent values:

- API URL, token id and secret, the Proxmox root CA (`/etc/pve/pve-root-ca.pem` from a node);
- node names, and key-based root ssh to each node from the machine that runs the lab (the templates are built over ssh);
- the three VNet or bridge names and the subnets above, the controller machine's address, and the template base id (default 9960).

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

The script has not run on real hardware yet. Expect to adjust it on the first run; results then go into
[adr/0056](../adr/0056-end-to-end-lab.md) and [production.md](../production.md), section 10.

## Tearing down

Delete the lab guests (ids in `hosts.env` and the engines the controller created), remove the SDN zone `lab`
(`pvesh delete /cluster/sdn/zones/lab` after its VNets), and `pveum user token remove switchyard@pve controller`.

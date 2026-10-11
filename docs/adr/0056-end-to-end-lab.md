# 0056. End-to-end lab on a real Proxmox

Status: Accepted
Date: 2026-10-11

## Context
06 listed the lab end-to-end test as not started. ADR 0054 gave a real Proxmox API (dockur/proxmox) and ADR 0055 LXC
engines, which run where there is no KVM.

## Decision
`deploy/lab/e2e/run.sh` runs the whole chain on one Docker host, about eight minutes:
1. Build the controller image and the SE image (its Docker checks run), and the SE **container template** inside the lab
   Proxmox with `se-image/lxc/build-template.sh`.
2. Prepare OpenBao (a PKI mount and an AppRole with the generated policy), certificates, a bundle key, a database and a
   tenant adopted from `tenant.yaml`.
3. Start the controller (api, worker, a TLS front) in the Proxmox container's network namespace, so it reaches the
   engines on the Proxmox bridge.
4. Through the API: deploy one LXC engine (a controller job against the real Proxmox), enroll it over https, renew its
   certificate, apply a virtual service (render, validate, canary, push through the Data Plane API over mTLS, VRRP via
   Keepalived), and request the VIP from the bridge: a 200 from a pool member through HAProxy in the container.
`teardown.sh` removes it all.

## What it found (and this change fixes)
Image: the agent unit needed `/run/switchyard` that does not exist after boot; Keepalived's stock unit looked in
`/etc/keepalived`, needed the `keepalived_script` user, and its state directory had to be writable by that user; HAProxy
must run the bootstrap config so reloads work; the Data Plane API port (5555) did not match the controller's default
(5556) and had no usable credentials; the Data Plane API must restart after a certificate renewal.
OpenBao: the agent role must set `use_csr_sans=false`, or OpenBao ignores the SAN the controller sends and the engine's
IP never reaches the certificate (the agent could also have chosen its own names).
Controller: `GET /status/engines` answered 500 as soon as an engine had pool members, because the member model forbade the
health-check fields the service reports.

## Two engines, failover and BGP (second step)
`scenario.sh` now runs on three lab networks (management, VIP, backend; ADR 0057 gives the VIP and backend NICs their
addresses): two VRRP engines (canary engine first, then the rest, each with its own signed bundle), killing the MASTER (the VIP
answers again within seconds on the other engine and the old MASTER takes it back), rebooting both engines with the controller
stopped (they come back serving), and a BGP group with one engine peering with an upstream FRR container (route announced,
reachable through BGP, withdrawn when HAProxy stops, restored when it starts).

## Not covered
Real KVM VMs and cloud-init, BGP (no peer in the lab), IPVS/UDP (the lab kernel has none), more than one engine (so no
canary ordering or VRRP failover), the UI, certificates for virtual services, and the multi-node controller.
The data-NIC addressing of VIP and backend networks is not handled by the deploy job at all (it only sets the
management address); the lab puts every role on one network, so this gap stays open.

# Kubernetes connector on a real cluster (2026-10-12)

Checked against `main` on the two-host Proxmox lab ([runbooks/lab-hosts.md](../runbooks/lab-hosts.md)).

## Cluster

k3s v1.36.5, one server (CT 9990, pve01) and one agent (CT 9991, pve02), both privileged LXC containers on the
`labmgmt` (172.30.0.30/.31) and `labbe` (10.98.0.30/.31) overlays. traefik and servicelb are disabled. Gateway API
CRDs v1.2.1. Demo app: Deployment `web` (2 × nginx), Service `web` (`loadBalancerClass: switchyard.io/lb`), ClusterIP
Service `web-internal`, GatewayClass `switchyard`, Gateway `gw` (host `app.lab.test`) and an HTTPRoute to `web-internal`.
The connector uses a ServiceAccount token with the ClusterRole from [kubernetes.md](../kubernetes.md).

Containers need `lxc.mount.entry: /dev/kmsg dev/kmsg none bind,optional,create=file` or the kubelet exits.

## Results

| Step | Result |
| --- | --- |
| Register cluster (`se_group`, `vip_pool`, CA) | ok |
| `POST .../kubernetes-clusters/{id}/test` | ok: version, 2 nodes, 5 services, `gateway_api: true`, all six permissions true |
| Sync, `nodeport` mode | change set by `system:k8s` applied; VIP `10.99.0.200` written to the Service's `status.loadBalancer`; `curl` through the VIP returns 200 |
| HTTPRoute to a ClusterIP Service, `nodeport` mode | not planned; note `httproute:demo/web: has no nodePort ... use pods mode` (as designed) |
| Sync, `pods` mode | second change set applied; pool members are the pod IPs `10.42.0.5` and `10.42.1.2`; Gateway virtual service `k8s-k3s--gw-demo-gw` on `10.99.0.201` with a host route; both VIPs return 200 |

In `pods` mode the engines need a route to the pod network (`10.42.0.0/16 via 10.98.0.30`); without it HAProxy
accepted the connection and closed it (empty reply). The routes were added by hand on the two engines.

## Findings

- The controller's Docker network used MTU 1500 over a 1430 overlay, so the TLS handshake from the API container to
  the cluster timed out while `curl` on the host worked. Setting the quickstart network MTU (1400) fixed it. Worth a
  note in the quickstart for overlay or tunnel underlays.
- The Gateway keeps `Accepted` and `Programmed` at `Unknown` ("Waiting for controller"): the connector writes the VIP
  to Services only, not to `Gateway.status.addresses` or the conditions. Tools that wait for a Gateway to be
  programmed (for example `kubectl wait`, cert-manager's Gateway solver) will not see it ready.
- The test endpoint path takes the cluster id, not its name, although [kubernetes.md](../kubernetes.md) writes
  `{cluster}`.
- `last_sync` describes the most recent run; a repeat sync with nothing to do overwrites the counts of the run that
  made the change (`created: 0` right after a change set was applied).

Not run: Service deletion and cleanup, drift on a deleted VIP, token rotation.

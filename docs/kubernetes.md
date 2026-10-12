# Kubernetes connector

Switchyard publishes Kubernetes `Service`s of type `LoadBalancer` and Gateway API `Gateway`s on its
service engines (ADR 0151). Nothing is installed in the cluster.

## Connect a cluster

1. Create a service account and grant it read access plus `patch` on `services/status` (and
   `gateways/status` when you use Gateways):

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: switchyard}
rules:
  - apiGroups: [""]
    resources: [nodes, services, endpoints]
    verbs: [get, list]
  - apiGroups: [""]
    resources: [services/status]
    verbs: [patch]
  - apiGroups: [discovery.k8s.io]
    resources: [endpointslices]
    verbs: [get, list]
  - apiGroups: [gateway.networking.k8s.io]
    resources: [gateways, httproutes]
    verbs: [get, list]
  - apiGroups: [gateway.networking.k8s.io]
    resources: [gateways/status]
    verbs: [patch]
```

2. Register it: `POST /tenants/{t}/kubernetes-clusters` with `name`, `api_url`, `token`, optional
   `ca_pem`, `se_group`, `vip_pool`, `mode` (`nodeport` or `pods`), `class_name`
   (default `switchyard.io/lb`) and `gateway_class` (default `switchyard`).
   Private API servers need their network in `SWITCHYARD_K8S_CIDRS`.
3. `POST /tenants/{t}/kubernetes-clusters/{id}/test` (the cluster's **id**, not its name) reports the
   version, counts and missing permissions. The Gateway API permissions are checked when its CRDs are
   installed.

## What is created

| Kubernetes | Switchyard |
| --- | --- |
| `Service` with our `loadBalancerClass`, each port | TCP/UDP virtual service `k8s-<cluster>--<ns>-<name>-<port>` and a pool |
| `Gateway` of our class with `HTTPRoute`s | one HTTP service `k8s-<cluster>--gw-<ns>-<name>` with host/path routes; certificate from annotation `switchyard.io/certificate` |

All ports of one Service share one VIP. In `nodeport` mode (the default) the pool members are the
nodes' addresses and node ports, so the engines only need to reach the nodes. In **`pods` mode** the members
are pod addresses from the EndpointSlices: the engines must be able to route to the pod network (a CNI that
advertises pod CIDRs by BGP, or a static route on the engines); otherwise every member is down.

Changes arrive as a change set opened by `system:k8s`;
`POST .../{cluster}/sync` (or `syctl api k8s-sync NAME --tenant T`) syncs now. After commit the VIP is written to
the Service's `status.loadBalancer`, and a Gateway gets `status.addresses` and the `Accepted` and
`Programmed` conditions (so `kubectl get gateway` shows the address instead of "Waiting for controller").
`last_sync` shows the latest sync; `last_sync.last_change` keeps the counts and change set of the last sync
that changed anything, which a later sync with nothing to do does not overwrite. Deleting a Service removes its objects; only names with the
cluster's prefix are touched.

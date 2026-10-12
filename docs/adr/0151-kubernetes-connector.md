# ADR 0151: Kubernetes connector

Status: Accepted

## Context

Teams on Kubernetes need load balancers for `Service` objects and Gateways without a cloud
provider. Avi did this with an in-cluster controller. Switchyard must not put a component in the
cluster, and the data plane must not depend on the controller.

## Decision

- The controller **polls** each registered cluster (every 30 s, or on demand) with a read-only
  service-account token held in OpenBao. Nothing runs in the cluster.
- A **pure planner** maps `Service` (type `LoadBalancer`, our `loadBalancerClass`) and Gateway API
  `Gateway`/`HTTPRoute` to pools and virtual services named `k8s-<cluster>--...`. Only objects with
  that prefix are ever changed or deleted.
- Backends are node ports on ready nodes (`nodeport`, default) or pod addresses from EndpointSlices
  (`pods`).
- The sync diffs the plan against the snapshot and opens **one change set** as `system:k8s`, going
  through the normal pipeline (validation, canary, rollout). At most one change set per cluster is in
  flight.
- One VIP is allocated per Kubernetes Service and kept while the Service exists. Once its virtual
  service is committed, the VIP is written to `status.loadBalancer` (the only write to the cluster).
- Failures are recorded on the cluster and never stop other clusters. API servers pass the same SSRF
  policy as DNS servers (`SWITCHYARD_K8S_CIDRS`). Permission: `manage kubernetes_cluster`, tenant-admin.

## Consequences

Up to 30 s of delay between a Service change and its change set. Drift is reported, not corrected.
UDP Services get a UDP health monitor, TCP and HTTP ones a TCP monitor.

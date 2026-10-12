# Kubernetes

**What.** The controller polls registered clusters and turns `Service` objects of type `LoadBalancer` (with Switchyard's
`loadBalancerClass`) and Gateway API `Gateway`/`HTTPRoute` objects into pools and virtual services, then writes the VIP back
to `status.loadBalancer`. Nothing runs in the cluster.

**How to use.** Create a read-only service account, store its token in OpenBao, and register the cluster with
`POST .../kubernetes-clusters`; `.../test` checks access and `.../sync` syncs now. The full guide:
[Kubernetes](../kubernetes.md).

**Limits.** Only objects named `k8s-<cluster>--...` are ever changed or deleted. Backends are node ports (default) or pod
addresses. One VIP per Kubernetes Service. API server addresses must pass `SWITCHYARD_K8S_CIDRS`. Lab results:
[docs/lab](../lab/2026-10-12-kubernetes.md).

**Decision:** [0151](../adr/0151-kubernetes-connector.md).

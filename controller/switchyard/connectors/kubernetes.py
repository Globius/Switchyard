"""Plan Switchyard objects from the state of a Kubernetes cluster (D2, ADR 0151).

A pure function over the JSON the API server returns. It reads:

- **Services** of type ``LoadBalancer`` whose ``loadBalancerClass`` is the cluster's class
  (default ``switchyard.io/lb``): one TCP or UDP virtual service per Service port, all with one
  VIP per Service, each with a pool of the cluster's nodes at the NodePort (``nodeport`` mode) or
  of the ready endpoints (``pods`` mode, for pod networks the SEs can reach).
- **Gateways** whose class is the cluster's gateway class (default ``switchyard``) and the
  **HTTPRoutes** attached to them: one HTTP virtual service per Gateway with a route per
  hostname and match, pools per backend Service port. An HTTPS listener needs the annotation
  ``switchyard.io/certificate: <certificate name>``; Secrets are never read.

Everything it plans is named ``k8s-<cluster>-...``, which is how the connector recognises (and may
delete) what it created. Anything it cannot map becomes a finding.

Example:
    >>> object_name("k8s-prod-", "web", "frontend", "443")
    'k8s-prod-web-frontend-443'
    >>> len(object_name("k8s-prod-", "n" * 40, "s" * 40, "80")) <= 63
    True
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

CERTIFICATE_ANNOTATION = "switchyard.io/certificate"
EXCLUDE_LABEL = "node.kubernetes.io/exclude-from-external-load-balancers"
Mode = Literal["nodeport", "pods"]


@dataclass(frozen=True)
class ClusterSpec:
    """How the connector reads one cluster.

    Attributes:
        name: The cluster's name here.
        mode: ``nodeport`` (nodes at the NodePort) or ``pods`` (ready endpoints).
        class_name: ``loadBalancerClass`` of the Services that are ours.
        gateway_class: ``gatewayClassName`` of the Gateways that are ours.
    """

    name: str
    mode: Mode = "nodeport"
    class_name: str = "switchyard.io/lb"
    gateway_class: str = "switchyard"

    @property
    def prefix(self) -> str:
        """Prefix of every object name the connector owns."""
        return f"k8s-{self.name}-"


@dataclass
class PoolPlan:
    """A pool to have: members as ``(address, port)``."""

    name: str
    members: list[tuple[str, int]]


@dataclass
class ServicePlan:
    """A virtual service to have.

    Attributes:
        name: Object name.
        protocol: ``tcp``, ``udp`` or ``http``.
        vip_key: Services with the same key share one VIP (all ports of a Kubernetes Service).
        ports: Port bodies (``{"port": 80}`` or with ``tls``).
        default_pool: Pool name for tcp/udp.
        routes: ``http`` routes, best match first: ``{"match": {...}, "pool": name}``.
        certificate: Name of the certificate for TLS ports.
        source: ``(kind, namespace, name)`` of the Kubernetes object.
    """

    name: str
    protocol: str
    vip_key: str
    ports: list[dict[str, Any]]
    default_pool: str | None = None
    routes: list[dict[str, Any]] = field(default_factory=list)
    certificate: str | None = None
    source: tuple[str, str, str] = ("", "", "")


@dataclass(frozen=True)
class Note:
    """A decision worth telling the administrator about."""

    level: Literal["info", "warning"]
    object: str
    message: str


@dataclass
class K8sPlan:
    """What the cluster asks for."""

    pools: dict[str, PoolPlan] = field(default_factory=dict)
    services: dict[str, ServicePlan] = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    #: ``vip_key -> (namespace, service)`` for the Services that get their address written back.
    status_targets: dict[str, tuple[str, str]] = field(default_factory=dict)

    def note(self, level: Literal["info", "warning"], obj: str, message: str) -> None:
        """Record a note."""
        self.notes.append(Note(level, obj, message))


def object_name(prefix: str, *parts: str) -> str:
    """A valid object name (at most 63 characters; long names are shortened with a hash)."""
    raw = prefix + "-".join(parts)
    clean = re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-_")
    if len(clean) <= 63:
        return clean
    digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return clean[:54].rstrip("-_") + "-" + digest


def _meta(obj: Mapping[str, Any]) -> tuple[str, str]:
    meta = obj.get("metadata") or {}
    return str(meta.get("namespace", "default")), str(meta.get("name", ""))


def _node_address(node: Mapping[str, Any]) -> str | None:
    addresses = (node.get("status") or {}).get("addresses") or []
    for kind in ("InternalIP", "ExternalIP"):
        for a in addresses:
            if a.get("type") == kind and a.get("address"):
                return str(a["address"])
    return None


def _node_ready(node: Mapping[str, Any]) -> bool:
    conditions = (node.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def usable_nodes(nodes: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """``(name, address)`` of the ready nodes that take external load balancer traffic."""
    out = []
    for node in nodes:
        labels = (node.get("metadata") or {}).get("labels") or {}
        address = _node_address(node)
        if address and _node_ready(node) and EXCLUDE_LABEL not in labels:
            out.append((str((node.get("metadata") or {}).get("name", "")), address))
    return sorted(out)


def _slices_of(
    slices: Sequence[Mapping[str, Any]], namespace: str, service: str
) -> list[Mapping[str, Any]]:
    return [
        s
        for s in slices
        if (s.get("metadata") or {}).get("namespace") == namespace
        and ((s.get("metadata") or {}).get("labels") or {}).get("kubernetes.io/service-name")
        == service
    ]


def _endpoints(
    slices: Sequence[Mapping[str, Any]],
    namespace: str,
    service: str,
    port_name: str | None,
    target: Any,
) -> list[tuple[str, int, str | None]]:
    """Ready endpoints as ``(address, port, node name)`` for one Service port."""
    out: list[tuple[str, int, str | None]] = []
    for sl in _slices_of(slices, namespace, service):
        if sl.get("addressType") not in {"IPv4", "IPv6"}:
            continue
        port = next(
            (
                p
                for p in sl.get("ports") or []
                if (p.get("name") or None) == (port_name or None)
                or (isinstance(target, int) and p.get("port") == target)
            ),
            None,
        )
        if port is None or not port.get("port"):
            continue
        for ep in sl.get("endpoints") or []:
            if (ep.get("conditions") or {}).get("ready") is False:
                continue
            for address in ep.get("addresses") or []:
                out.append((str(address), int(port["port"]), ep.get("nodeName")))
    return sorted(set(out), key=lambda e: (e[0], e[1]))


def plan(
    spec: ClusterSpec,
    nodes: Sequence[Mapping[str, Any]],
    services: Sequence[Mapping[str, Any]],
    slices: Sequence[Mapping[str, Any]],
    gateways: Sequence[Mapping[str, Any]] | None = None,
    routes: Sequence[Mapping[str, Any]] | None = None,
) -> K8sPlan:
    """Plan the objects the cluster asks for. See the module docstring for the rules."""
    result = K8sPlan()
    ready = usable_nodes(nodes)
    by_key = {_meta(s): s for s in services}

    def members_for(
        svc: Mapping[str, Any], port: Mapping[str, Any], label: str
    ) -> list[tuple[str, int]] | None:
        namespace, name = _meta(svc)
        spec_ = svc.get("spec") or {}
        if spec.mode == "pods":
            eps = _endpoints(
                slices, namespace, name, port.get("name"), port.get("targetPort", port.get("port"))
            )
            return [(a, p) for a, p, _ in eps]
        node_port = port.get("nodePort")
        if not node_port:
            result.note(
                "warning",
                label,
                "has no nodePort (allocateLoadBalancerNodePorts is false?): use pods mode",
            )
            return None
        nodes_ = ready
        if spec_.get("externalTrafficPolicy") == "Local":
            hosting = {
                n
                for _, _, n in _endpoints(
                    slices,
                    namespace,
                    name,
                    port.get("name"),
                    port.get("targetPort", port.get("port")),
                )
                if n
            }
            nodes_ = [(n, a) for n, a in ready if n in hosting]
        return [(address, int(node_port)) for _, address in nodes_]

    def pool_for(
        svc: Mapping[str, Any], port: Mapping[str, Any], label: str, name: str
    ) -> str | None:
        members = members_for(svc, port, label)
        if members is None:
            return None
        if not members:
            result.note("warning", label, "no ready backend yet: the pool is empty")
        result.pools[name] = PoolPlan(name, members)
        return name

    # -- Services of type LoadBalancer ----------------------------------------------------------
    for svc in services:
        spec_ = svc.get("spec") or {}
        if spec_.get("type") != "LoadBalancer" or spec_.get("loadBalancerClass") != spec.class_name:
            continue
        namespace, name = _meta(svc)
        vip_key = f"svc/{namespace}/{name}"
        planned = False
        for port in spec_.get("ports") or []:
            number = int(port.get("port", 0))
            label = f"service:{namespace}/{name}:{number}"
            protocol = str(port.get("protocol", "TCP")).upper()
            if protocol not in {"TCP", "UDP"}:
                result.note("warning", label, f"{protocol} is not supported")
                continue
            object_ = object_name(spec.prefix, namespace, name, str(number))
            pool = pool_for(svc, port, label, object_)
            if pool is None:
                continue
            result.services[object_] = ServicePlan(
                object_,
                protocol.lower(),
                vip_key,
                [{"port": number}],
                pool,
                source=("Service", namespace, name),
            )
            planned = True
        if planned:
            result.status_targets[vip_key] = (namespace, name)

    # -- Gateway API -----------------------------------------------------------------------------
    for gateway in gateways or []:
        if (gateway.get("spec") or {}).get("gatewayClassName") != spec.gateway_class:
            continue
        _plan_gateway(spec, gateway, routes or [], by_key, result, pool_for)
    return result


def _plan_gateway(
    spec: ClusterSpec,
    gateway: Mapping[str, Any],
    routes: Sequence[Mapping[str, Any]],
    services: Mapping[tuple[str, str], Mapping[str, Any]],
    result: K8sPlan,
    pool_for: Any,
) -> None:
    namespace, name = _meta(gateway)
    label = f"gateway:{namespace}/{name}"
    annotations = (gateway.get("metadata") or {}).get("annotations") or {}
    certificate = annotations.get(CERTIFICATE_ANNOTATION)
    ports: list[dict[str, Any]] = []
    for listener in (gateway.get("spec") or {}).get("listeners") or []:
        protocol = str(listener.get("protocol", "")).upper()
        number = int(listener.get("port", 0))
        if protocol == "HTTP":
            ports.append({"port": number})
        elif protocol == "HTTPS" and certificate:
            ports.append({"port": number, "tls": True})
        elif protocol == "HTTPS":
            result.note(
                "warning",
                label,
                f"HTTPS listener {number} needs the annotation {CERTIFICATE_ANNOTATION}",
            )
        else:
            result.note("warning", label, f"{protocol or '?'} listener {number} is not supported")
    if not ports:
        result.note("warning", label, "no usable listener: nothing planned")
        return
    entries: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    for route in routes:
        r_ns, r_name = _meta(route)
        attached = any(
            ref.get("name") == name and ref.get("namespace", r_ns) == namespace
            for ref in (route.get("spec") or {}).get("parentRefs") or []
        )
        if not attached:
            continue
        names = [str(h).lower() for h in (route.get("spec") or {}).get("hostnames") or []]
        hostnames: list[str | None] = [*names] if names else [None]
        for rule in (route.get("spec") or {}).get("rules") or []:
            backends = rule.get("backendRefs") or []
            if not backends:
                result.note(
                    "warning", f"httproute:{r_ns}/{r_name}", "a rule without backends is skipped"
                )
                continue
            backend = max(backends, key=lambda b: b.get("weight", 1))
            if len(backends) > 1:
                result.note(
                    "warning",
                    f"httproute:{r_ns}/{r_name}",
                    "several backends: only the heaviest is used",
                )
            svc = services.get((backend.get("namespace", r_ns), str(backend.get("name"))))
            if svc is None:
                result.note(
                    "warning",
                    f"httproute:{r_ns}/{r_name}",
                    f"backend {backend.get('name')} not found",
                )
                continue
            port = next(
                (
                    p
                    for p in (svc.get("spec") or {}).get("ports") or []
                    if p.get("port") == backend.get("port")
                ),
                None,
            )
            if port is None:
                result.note(
                    "warning",
                    f"httproute:{r_ns}/{r_name}",
                    f"backend port {backend.get('port')} not found",
                )
                continue
            b_ns, b_name = _meta(svc)
            pool_name = object_name(spec.prefix, "gw", b_ns, b_name, str(port["port"]))
            if (
                pool_name not in result.pools
                and pool_for(svc, port, f"httproute:{r_ns}/{r_name}", pool_name) is None
            ):
                continue
            for match in rule.get("matches") or [{}]:
                path = match.get("path") or {"type": "PathPrefix", "value": "/"}
                kind = str(path.get("type", "PathPrefix"))
                value = str(path.get("value", "/"))
                if kind not in {"PathPrefix", "Exact"} or not re.fullmatch(
                    r"/[A-Za-z0-9._~/%:@+,;=-]*", value
                ):
                    result.note(
                        "warning",
                        f"httproute:{r_ns}/{r_name}",
                        f"path match {kind} {value!r} is not supported",
                    )
                    continue
                if any(match.get(k) for k in ("headers", "queryParams", "method")):
                    result.note(
                        "warning",
                        f"httproute:{r_ns}/{r_name}",
                        "header, query and method matches are not supported",
                    )
                    continue
                for host in hostnames:
                    rm: dict[str, Any] = {"path_exact" if kind == "Exact" else "path_prefix": value}
                    if host:
                        rm["host"] = host
                    rank = (
                        0 if kind == "Exact" else 1,
                        -len(value),
                        0 if host and not host.startswith("*.") else 1,
                    )
                    entries.append((rank, {"match": rm, "pool": pool_name}))
    entries.sort(key=lambda e: e[0])
    object_ = object_name(spec.prefix, "gw", namespace, name)
    if not entries:
        result.note("info", label, "no routes attached yet")
        return
    result.services[object_] = ServicePlan(
        object_,
        "http",
        f"gw/{namespace}/{name}",
        ports,
        routes=[e[1] for e in entries],
        certificate=str(certificate) if certificate else None,
        source=("Gateway", namespace, name),
    )

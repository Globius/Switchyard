"""Kubernetes API client for the connector (D2, ADR 0151).

Read-only except for one call: writing the VIP back to ``Service.status.loadBalancer``. The client
speaks plain REST with a bearer token (a service account), verifies the API server's certificate
against the CA given in the cluster's settings, and returns the raw JSON objects; the planner
(``switchyard.connectors.kubernetes``) reads them. :class:`FakeKube` is an in-memory cluster
for tests.

Needed RBAC (``docs/kubernetes.md`` has the manifest): ``get, list`` on ``nodes``, ``services``,
``endpointslices``, ``gateways`` and ``httproutes``; ``patch`` on ``services/status`` and
``gateways/status``.
"""

import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

GATEWAY_API = "/apis/gateway.networking.k8s.io/v1"
DISCOVERY_API = "/apis/discovery.k8s.io/v1"


class KubeError(Exception):
    """A Kubernetes API call failed."""


class Kube(Protocol):
    """What the connector needs from a cluster."""

    async def version(self) -> str:
        """The server's version string (``v1.31.2``)."""
        ...

    async def nodes(self) -> list[dict[str, Any]]:
        """Every node."""
        ...

    async def services(self) -> list[dict[str, Any]]:
        """Every service of every namespace."""
        ...

    async def endpoint_slices(self) -> list[dict[str, Any]]:
        """Every EndpointSlice of every namespace."""
        ...

    async def gateways(self) -> list[dict[str, Any]] | None:
        """Gateway API gateways, or ``None`` when the CRDs are not installed."""
        ...

    async def http_routes(self) -> list[dict[str, Any]] | None:
        """Gateway API HTTPRoutes, or ``None`` when the CRDs are not installed."""
        ...

    async def set_service_address(self, namespace: str, name: str, address: str | None) -> None:
        """Set (or clear, with ``None``) ``status.loadBalancer.ingress`` of a service."""
        ...

    async def set_gateway_status(
        self, namespace: str, name: str, address: str | None, generation: int
    ) -> None:
        """Mark a Gateway accepted and programmed with ``address`` (cleared with ``None``)."""
        ...

    async def can(self, verb: str, resource: str, group: str = "", subresource: str = "") -> bool:
        """Whether the token may do ``verb`` on ``resource`` (a SelfSubjectAccessReview)."""
        ...

    async def close(self) -> None:
        """Release connections."""
        ...


class HttpKube:
    """The real client."""

    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        ca_pem: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        """Create a client for ``api_url`` (``https://host:6443``) with a service-account token."""
        verify: ssl.SSLContext | bool = True
        if ca_pem:
            verify = ssl.create_default_context(cadata=ca_pem)
        self._http = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            verify=verify,
            transport=transport,
            timeout=timeout_s,
        )

    async def _get(self, path: str, *, optional: bool = False) -> Any:
        try:
            response = await self._http.get(path)
        except httpx.HTTPError as exc:
            raise KubeError(f"GET {path}: {exc}") from exc
        if optional and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise KubeError(f"GET {path}: HTTP {response.status_code}: {_reason(response)}")
        return response.json()

    async def _list(self, path: str, *, optional: bool = False) -> list[dict[str, Any]] | None:
        items: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            suffix = f"?limit=500&continue={token}" if token else "?limit=500"
            body = await self._get(path + suffix, optional=optional)
            if body is None:
                return None
            items += body.get("items", [])
            token = (body.get("metadata") or {}).get("continue")
            if not token:
                return items

    async def version(self) -> str:
        """The server version."""
        return str((await self._get("/version")).get("gitVersion", "unknown"))

    async def nodes(self) -> list[dict[str, Any]]:
        """Every node."""
        return await self._list("/api/v1/nodes") or []

    async def services(self) -> list[dict[str, Any]]:
        """Every service."""
        return await self._list("/api/v1/services") or []

    async def endpoint_slices(self) -> list[dict[str, Any]]:
        """Every EndpointSlice."""
        return await self._list(f"{DISCOVERY_API}/endpointslices") or []

    async def gateways(self) -> list[dict[str, Any]] | None:
        """Gateways, or ``None`` without the Gateway API."""
        return await self._list(f"{GATEWAY_API}/gateways", optional=True)

    async def http_routes(self) -> list[dict[str, Any]] | None:
        """HTTPRoutes, or ``None`` without the Gateway API."""
        return await self._list(f"{GATEWAY_API}/httproutes", optional=True)

    async def set_service_address(self, namespace: str, name: str, address: str | None) -> None:
        """Write the address back to the service status."""
        ingress = [{"ip": address}] if address else []
        path = f"/api/v1/namespaces/{namespace}/services/{name}/status"
        try:
            response = await self._http.patch(
                path,
                json={"status": {"loadBalancer": {"ingress": ingress}}},
                headers={"Content-Type": "application/merge-patch+json"},
            )
        except httpx.HTTPError as exc:
            raise KubeError(f"PATCH {path}: {exc}") from exc
        if response.status_code >= 400:
            raise KubeError(f"PATCH {path}: HTTP {response.status_code}: {_reason(response)}")

    async def set_gateway_status(
        self, namespace: str, name: str, address: str | None, generation: int
    ) -> None:
        """Write ``status.addresses`` and the Accepted and Programmed conditions of a Gateway."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ok = address is not None

        def condition(kind: str, reason: str, message: str) -> dict[str, Any]:
            return {
                "type": kind,
                "status": "True" if ok else "False",
                "reason": reason if ok else "Pending",
                "message": message,
                "observedGeneration": generation,
                "lastTransitionTime": now,
            }

        status = {
            "addresses": [{"type": "IPAddress", "value": address}] if address else [],
            "conditions": [
                condition("Accepted", "Accepted", "accepted by Switchyard"),
                condition("Programmed", "Programmed", "served by the service engines"),
            ],
        }
        path = f"{GATEWAY_API}/namespaces/{namespace}/gateways/{name}/status"
        try:
            response = await self._http.patch(
                path,
                json={"status": status},
                headers={"Content-Type": "application/merge-patch+json"},
            )
        except httpx.HTTPError as exc:
            raise KubeError(f"PATCH {path}: {exc}") from exc
        if response.status_code >= 400:
            raise KubeError(f"PATCH {path}: HTTP {response.status_code}: {_reason(response)}")

    async def can(self, verb: str, resource: str, group: str = "", subresource: str = "") -> bool:
        """Ask the API server whether this token may do something."""
        review = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectAccessReview",
            "spec": {
                "resourceAttributes": {
                    "verb": verb,
                    "resource": resource,
                    "group": group,
                    "subresource": subresource,
                }
            },
        }
        try:
            response = await self._http.post(
                "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", json=review
            )
        except httpx.HTTPError as exc:
            raise KubeError(f"access review: {exc}") from exc
        if response.status_code >= 400:
            raise KubeError(f"access review: HTTP {response.status_code}: {_reason(response)}")
        return bool((response.json().get("status") or {}).get("allowed"))

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()


def _reason(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", response.text))[:200]
    except ValueError:
        return response.text[:200]


@dataclass
class FakeKube:
    """An in-memory cluster.

    Attributes:
        node_list: ``nodes()`` answers.
        service_list: ``services()`` answers.
        slice_list: ``endpoint_slices()`` answers.
        gateway_list: ``gateways()`` answers; ``None`` means the Gateway API is not installed.
        route_list: ``http_routes()`` answers; ``None`` likewise.
        statuses: ``(namespace, name) -> address`` written by the connector.
        allowed: Access reviews that answer ``False``, as ``verb/resource/subresource``.
        fail: Operation names that raise once.
    """

    node_list: list[dict[str, Any]] = field(default_factory=list)
    service_list: list[dict[str, Any]] = field(default_factory=list)
    slice_list: list[dict[str, Any]] = field(default_factory=list)
    gateway_list: list[dict[str, Any]] | None = None
    route_list: list[dict[str, Any]] | None = None
    statuses: dict[tuple[str, str], str | None] = field(default_factory=dict)
    gateway_statuses: dict[tuple[str, str], str | None] = field(default_factory=dict)
    denied: set[str] = field(default_factory=set)
    fail: set[str] = field(default_factory=set)
    closed: bool = False

    def _check(self, op: str) -> None:
        if op in self.fail:
            self.fail.discard(op)
            raise KubeError(f"injected failure in {op}")

    async def version(self) -> str:
        """A fixed version."""
        self._check("version")
        return "v1.31.0"

    async def nodes(self) -> list[dict[str, Any]]:
        """The configured nodes."""
        self._check("nodes")
        return self.node_list

    async def services(self) -> list[dict[str, Any]]:
        """The configured services."""
        self._check("services")
        return self.service_list

    async def endpoint_slices(self) -> list[dict[str, Any]]:
        """The configured slices."""
        self._check("slices")
        return self.slice_list

    async def gateways(self) -> list[dict[str, Any]] | None:
        """The configured gateways."""
        return self.gateway_list

    async def http_routes(self) -> list[dict[str, Any]] | None:
        """The configured routes."""
        return self.route_list

    async def set_service_address(self, namespace: str, name: str, address: str | None) -> None:
        """Record the address."""
        self._check("status")
        self.statuses[(namespace, name)] = address
        for item in self.service_list:
            meta = item["metadata"]
            if (meta["namespace"], meta["name"]) == (namespace, name):
                ingress = [{"ip": address}] if address else []
                item["status"] = {"loadBalancer": {"ingress": ingress}}

    async def set_gateway_status(
        self, namespace: str, name: str, address: str | None, generation: int
    ) -> None:
        """Record the address and mark the gateway programmed."""
        self._check("status")
        self.gateway_statuses[(namespace, name)] = address
        for item in self.gateway_list or []:
            meta = item["metadata"]
            if (meta["namespace"], meta["name"]) == (namespace, name):
                ok = "True" if address else "False"
                item["status"] = {
                    "addresses": [{"type": "IPAddress", "value": address}] if address else [],
                    "conditions": [
                        {"type": "Accepted", "status": ok},
                        {"type": "Programmed", "status": ok},
                    ],
                }

    async def can(self, verb: str, resource: str, group: str = "", subresource: str = "") -> bool:
        """Allowed unless listed in ``denied``."""
        return f"{verb}/{resource}/{subresource}" not in self.denied

    async def close(self) -> None:
        """Mark closed."""
        self.closed = True

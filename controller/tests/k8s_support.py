# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Kubernetes objects for the connector tests (JSON as the API server returns it)."""

from typing import Any


def node(
    name: str, address: str, *, ready: bool = True, labels: dict[str, str] | None = None
) -> dict[str, Any]:
    """A node."""
    return {
        "metadata": {"name": name, "labels": labels or {}},
        "status": {
            "addresses": [
                {"type": "InternalIP", "address": address},
                {"type": "Hostname", "address": name},
            ],
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def service(
    namespace: str,
    name: str,
    ports: list[dict[str, Any]],
    *,
    kind: str = "LoadBalancer",
    klass: str | None = "switchyard.io/lb",
    policy: str = "Cluster",
) -> dict[str, Any]:
    """A service."""
    spec: dict[str, Any] = {"type": kind, "ports": ports, "externalTrafficPolicy": policy}
    if klass:
        spec["loadBalancerClass"] = klass
    return {"metadata": {"namespace": namespace, "name": name}, "spec": spec}


def endpoint_slice(
    namespace: str,
    service_name: str,
    ports: list[dict[str, Any]],
    endpoints: list[tuple[str, str | None, bool]],
) -> dict[str, Any]:
    """An EndpointSlice: endpoints are ``(address, node name, ready)``."""
    return {
        "metadata": {
            "namespace": namespace,
            "name": f"{service_name}-abc",
            "labels": {"kubernetes.io/service-name": service_name},
        },
        "addressType": "IPv4",
        "ports": ports,
        "endpoints": [
            {"addresses": [a], "nodeName": n, "conditions": {"ready": r}} for a, n, r in endpoints
        ],
    }


def gateway(
    namespace: str,
    name: str,
    listeners: list[dict[str, Any]],
    *,
    klass: str = "switchyard",
    certificate: str | None = None,
) -> dict[str, Any]:
    """A Gateway."""
    meta: dict[str, Any] = {"namespace": namespace, "name": name}
    if certificate:
        meta["annotations"] = {"switchyard.io/certificate": certificate}
    return {"metadata": meta, "spec": {"gatewayClassName": klass, "listeners": listeners}}


def http_route(
    namespace: str,
    name: str,
    parent: str,
    rules: list[dict[str, Any]],
    *,
    hostnames: list[str] | None = None,
    parent_namespace: str | None = None,
) -> dict[str, Any]:
    """An HTTPRoute attached to one Gateway."""
    ref: dict[str, Any] = {"name": parent}
    if parent_namespace:
        ref["namespace"] = parent_namespace
    return {
        "metadata": {"namespace": namespace, "name": name},
        "spec": {"parentRefs": [ref], "hostnames": hostnames or [], "rules": rules},
    }

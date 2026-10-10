"""Test data builders. Addresses are RFC 5737 / RFC 1918 examples and hostnames are made up."""

import copy
from typing import Any

from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import Snapshot

BASE: dict[str, Any] = {
    "tenants": [{"id": "t-acme", "name": "acme"}],
    "networks": [
        {
            "id": "net-lb",
            "tenant_id": "t-acme",
            "name": "lb",
            "cidr": "192.0.2.0/24",
            "gateway": "192.0.2.1",
        },
        {"id": "net-be", "tenant_id": "t-acme", "name": "backend", "cidr": "10.20.0.0/24"},
    ],
    "vip_pools": [
        {
            "id": "vp-main",
            "tenant_id": "t-acme",
            "name": "main",
            "network_id": "net-lb",
            "ranges": [{"start": "192.0.2.10", "end": "192.0.2.50"}],
        }
    ],
    "se_groups": [
        {
            "id": "seg-1",
            "tenant_id": "t-acme",
            "name": "edge",
            "ha_mode": "vrrp",
            "networks": [
                {"network_id": "net-lb", "role": "vip"},
                {"network_id": "net-be", "role": "backend"},
            ],
        }
    ],
    "certificates": [
        {
            "id": "cert-wild",
            "tenant_id": "t-acme",
            "name": "wild-example-test",
            "source": "cloudflare_origin",
            "sans": ["*.example.test", "example.test"],
            "not_after": "2038-01-01T00:00:00+00:00",
            "secret_ref": "kv/tenants/acme/certs/wild-example-test",
        }
    ],
    "pools": [
        {
            "id": "pool-web",
            "tenant_id": "t-acme",
            "name": "web",
            "members": [{"address": "10.20.0.11", "port": 8080}],
        }
    ],
    "virtual_services": [
        {
            "id": "vs-web",
            "tenant_id": "t-acme",
            "se_group_id": "seg-1",
            "name": "web",
            "protocol": "http",
            "vip": "192.0.2.20",
            "vip_pool_id": "vp-main",
            "ports": [
                {"port": 443, "tls": True, "http2": True},
                {"port": 80, "https_redirect_port": 443},
            ],
            "certificate_ids": ["cert-wild"],
            "routes": [{"match": {"host": "app.example.test"}, "pool_id": "pool-web"}],
        }
    ],
}


def base_data() -> dict[str, Any]:
    """Return a fresh, valid, minimal snapshot mapping that tests can mutate."""
    return copy.deepcopy(BASE)


def base_snapshot() -> Snapshot:
    """Return the valid minimal snapshot."""
    return snapshot_from_mapping(base_data())

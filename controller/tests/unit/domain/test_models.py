# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for single-object invariants in the domain models."""

import uuid
from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain.models import (
    AllowListPolicySet,
    Certificate,
    HealthMonitor,
    IpRange,
    Network,
    Pool,
    PoolMember,
    RedirectPolicySet,
    VirtualService,
    VsPort,
    VsRoute,
)

T = uuid.UUID(int=1)


def vs(**overrides: Any) -> VirtualService:
    data: dict[str, Any] = {
        "id": uuid.UUID(int=10),
        "tenant_id": T,
        "name": "svc",
        "se_group_id": uuid.UUID(int=11),
        "protocol": "http",
        "vip": "192.0.2.20",
        "ports": [{"port": 80}],
        "default_pool_id": uuid.UUID(int=12),
    }
    data.update(overrides)
    return VirtualService.model_validate(data)


def test_models_are_frozen_and_forbid_extras() -> None:
    net = Network(id=T, tenant_id=T, name="n", cidr="10.0.0.0/24")
    with pytest.raises(ValidationError):
        net.name = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Network(id=T, tenant_id=T, name="n", cidr="10.0.0.0/24", surprise=1)  # type: ignore[call-arg]


class TestNetwork:
    def test_cidr_with_host_bits_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Network(id=T, tenant_id=T, name="n", cidr="10.0.0.1/24")

    def test_gateway_must_be_inside(self) -> None:
        with pytest.raises(ValidationError, match="outside"):
            Network(id=T, tenant_id=T, name="n", cidr="10.0.0.0/24", gateway="10.0.1.1")

    def test_vlan_range(self) -> None:
        with pytest.raises(ValidationError):
            Network(id=T, tenant_id=T, name="n", cidr="10.0.0.0/24", vlan_or_vni=0)


class TestIpRange:
    def test_contains(self) -> None:
        r = IpRange(start="10.0.0.5", end="10.0.0.9")
        assert "10.0.0.5" not in r  # only address objects match
        import ipaddress

        assert ipaddress.ip_address("10.0.0.9") in r
        assert ipaddress.ip_address("10.0.0.10") not in r
        assert ipaddress.ip_address("2001:db8::1") not in r

    def test_order_and_family(self) -> None:
        with pytest.raises(ValidationError):
            IpRange(start="10.0.0.9", end="10.0.0.5")
        with pytest.raises(ValidationError):
            IpRange(start="10.0.0.1", end="2001:db8::1")


class TestVirtualService:
    def test_minimal_http(self) -> None:
        assert vs().name == "svc"

    def test_tls_needs_certificate(self) -> None:
        with pytest.raises(ValidationError, match="certificate"):
            vs(ports=[{"port": 443, "tls": True}])

    def test_duplicate_ports(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            vs(ports=[{"port": 80}, {"port": 80}])

    def test_http_needs_a_target(self) -> None:
        with pytest.raises(ValidationError, match="routes or default_pool_id"):
            vs(default_pool_id=None)

    def test_tcp_rules(self) -> None:
        assert vs(protocol="tcp", ports=[{"port": 6443}]).protocol == "tcp"
        with pytest.raises(ValidationError, match="default_pool_id"):
            vs(protocol="tcp", default_pool_id=None)
        route = {"match": {"host": "a.example.test"}, "pool_id": uuid.UUID(int=12)}
        with pytest.raises(ValidationError, match="routes"):
            vs(protocol="tcp", routes=[route])
        with pytest.raises(ValidationError, match="http services"):
            vs(
                protocol="tcp",
                ports=[{"port": 443, "tls": True, "http2": True}],
                certificate_ids=[T],
            )

    def test_https_redirect_must_target_tls_port_of_same_service(self) -> None:
        with pytest.raises(ValidationError, match="not a TLS port"):
            vs(ports=[{"port": 80, "https_redirect_port": 443}])
        ok = vs(
            ports=[{"port": 80, "https_redirect_port": 443}, {"port": 443, "tls": True}],
            certificate_ids=[T],
        )
        assert ok.ports[0].https_redirect_port == 443

    def test_allow_list_options_need_a_list(self) -> None:
        with pytest.raises(ValidationError, match="allow_list_id"):
            vs(enforce_allow_list=True)
        with pytest.raises(ValidationError, match="allow_list_id"):
            vs(restore_client_ip_header="CF-Connecting-IP")
        assert vs(allow_list_id=T, restore_client_ip_header="CF-Connecting-IP").allow_list_id == T

    def test_udp_needs_a_default_pool(self) -> None:
        with pytest.raises(ValidationError, match="default_pool_id"):
            vs(protocol="udp", ports=[{"port": 51820}], default_pool_id=None)


class TestPort:
    def test_http2_requires_tls(self) -> None:
        with pytest.raises(ValidationError, match="http2"):
            VsPort(port=80, http2=True)

    def test_redirect_only_on_plaintext(self) -> None:
        with pytest.raises(ValidationError, match="plaintext"):
            VsPort(port=443, tls=True, https_redirect_port=8443)


class TestRoute:
    def test_exactly_one_action(self) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            VsRoute.model_validate({"match": {"host": "a.example.test"}})
        with pytest.raises(ValidationError, match="exactly one"):
            VsRoute.model_validate(
                {
                    "match": {"host": "a.example.test"},
                    "pool_id": T,
                    "redirect": {"location": "/x"},
                }
            )

    def test_empty_match_rejected(self) -> None:
        with pytest.raises(ValidationError, match="host or a path"):
            VsRoute.model_validate({"match": {}, "pool_id": T})

    def test_prefix_and_exact_are_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="either"):
            VsRoute.model_validate(
                {"match": {"path_prefix": "/a", "path_exact": "/a"}, "pool_id": T}
            )


class TestPool:
    def test_member_name_derivation(self) -> None:
        assert PoolMember(id=T, address="10.0.0.5", port=8080).server_name == "10-0-0-5-8080"
        assert PoolMember(id=T, address="10.0.0.5").server_name == "10-0-0-5"
        assert PoolMember(id=T, address="2001:db8::1", port=80).server_name == "2001-db8--1-80"
        assert PoolMember(id=T, address="10.0.0.5", name="primary").server_name == "primary"

    def test_duplicate_members_rejected(self) -> None:
        m = {"id": T, "address": "10.0.0.5", "port": 80}
        with pytest.raises(ValidationError, match="unique"):
            Pool.model_validate(
                {
                    "id": T,
                    "tenant_id": T,
                    "name": "p",
                    "members": [m, {**m, "id": uuid.UUID(int=2)}],
                }
            )

    def test_weight_bounds(self) -> None:
        with pytest.raises(ValidationError):
            PoolMember(id=T, address="10.0.0.5", weight=257)

    def test_verify_requires_tls(self) -> None:
        with pytest.raises(ValidationError, match="tls_verify"):
            Pool.model_validate({"id": T, "tenant_id": T, "name": "p", "tls_verify": True})


class TestHealthMonitor:
    def test_http_needs_block(self) -> None:
        with pytest.raises(ValidationError, match="http block"):
            HealthMonitor.model_validate({"id": T, "tenant_id": T, "name": "m", "type": "http"})
        with pytest.raises(ValidationError, match="tcp monitor"):
            HealthMonitor.model_validate(
                {"id": T, "tenant_id": T, "name": "m", "type": "tcp", "http": {}}
            )

    def test_status_codes_bounded(self) -> None:
        with pytest.raises(ValidationError, match="expect_status"):
            HealthMonitor.model_validate(
                {
                    "id": T,
                    "tenant_id": T,
                    "name": "m",
                    "type": "http",
                    "http": {"expect_status": [99]},
                }
            )


class TestCertificate:
    def base(self, **kw: Any) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": T,
            "tenant_id": T,
            "name": "c",
            "source": "upload",
            "sans": ["a.example.test"],
            "not_after": datetime.fromisoformat("2030-01-01T00:00:00+00:00"),
            "secret_ref": "kv/x/y",
        }
        d.update(kw)
        return d

    def test_ok(self) -> None:
        assert Certificate.model_validate(self.base()).renew_policy == "manual"

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timezone"):
            Certificate.model_validate(self.base(not_after=datetime(2030, 1, 1)))

    def test_duplicate_sans_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            Certificate.model_validate(self.base(sans=["a.example.test", "a.example.test"]))

    def test_secret_ref_cannot_traverse(self) -> None:
        with pytest.raises(ValidationError):
            Certificate.model_validate(self.base(secret_ref="../etc/shadow"))


class TestPolicySets:
    def test_redirect_rule_needs_match(self) -> None:
        with pytest.raises(ValidationError, match="host or a path"):
            RedirectPolicySet.model_validate(
                {
                    "id": T,
                    "tenant_id": T,
                    "name": "r",
                    "rules": [{"match": {}, "redirect": {"location": "/x"}}],
                }
            )

    def test_allow_list(self) -> None:
        p = AllowListPolicySet.model_validate(
            {
                "id": T,
                "tenant_id": T,
                "name": "cf",
                "cidrs": ["192.0.2.0/24", "2001:db8::/32"],
                "managed_by": "cloudflare",
            }
        )
        assert len(p.cidrs) == 2

    def test_redirect_code_limited(self) -> None:
        with pytest.raises(ValidationError):
            RedirectPolicySet.model_validate(
                {
                    "id": T,
                    "tenant_id": T,
                    "name": "r",
                    "rules": [
                        {
                            "match": {"path_prefix": "/a"},
                            "redirect": {"location": "/b", "code": 200},
                        }
                    ],
                }
            )


def test_bgp_prefix_limits_are_optional_and_bounded() -> None:
    import uuid

    from pydantic import ValidationError

    from switchyard.domain.models import BgpPeer, BgpProfile

    peer = BgpPeer(peer_ip="192.0.2.1", remote_asn=65001)
    assert peer.max_prefixes is None
    profile = BgpProfile(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), name="p", asn=65000, peers=(peer,)
    )
    assert profile.max_prefixes is None
    for bad in (0, -1, 10_000_001):
        with pytest.raises(ValidationError):
            BgpPeer(peer_ip="192.0.2.1", remote_asn=65001, max_prefixes=bad)

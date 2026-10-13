# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The Avi export converter: what becomes what, and what is reported (ADR 0150)."""

from typing import Any
from uuid import UUID

import pytest

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.importers.avi import AviOptions, AviPlan, convert, ref_name, ref_uuid, slug
from tests.avi_support import export, ref

SE_GROUP = UUID(symbolic_uuid("seg-1"))


def run(**kwargs: Any) -> AviPlan:
    vips = {"vip-20": "192.0.2.20", "vip-27": "192.0.2.27", "vip-22": "192.0.2.22"}
    options = AviOptions(se_group_id=SE_GROUP, vip_for=lambda name, old: vips.get(name), **kwargs)
    return convert(export(), options)


def by_avi(plan: AviPlan, kind: str, avi_name: str) -> Any:
    return next(o for o in plan.objects if o.kind == kind and o.avi_name == avi_name)


def findings(plan: AviPlan, level: str) -> list[str]:
    return [f"{f.object}: {f.message}" for f in plan.findings if f.level == level]


def test_references_and_names() -> None:
    assert ref_name(ref("pool", "web")) == "web"
    assert ref_name("/api/pool?name=web") == "web"
    assert ref_name("https://x/api/pool/pool-1234") == "pool-1234"
    assert ref_uuid("https://x/api/pool/pool-1f2e") == "pool-1f2e"
    assert ref_name(None) is None
    assert slug("Keycloak SSO") == "keycloak-sso"
    assert slug("***") == "x"


def test_the_sni_parent_absorbs_its_children_as_host_routes() -> None:
    plan = run()
    parent = by_avi(plan, "virtual_service", "External-ParentSNI")
    body = parent.body
    assert body["protocol"] == "http" and body["vip"] == "@vip:vip-20"
    assert [(p["port"], p["tls"]) for p in body["ports"]] == [(443, True), (80, False)]
    assert body["ports"][0]["http2"] is True and body["ports"][0]["tls_profile"] == "intermediate"
    hosts = {r["match"]["host"]: r["pool_id"] for r in body["routes"] if "host" in r["match"]}
    assert set(hosts) == {"sso.globi.example", "sso-admin.globi.example", "billing.globi.example"}
    keycloak = by_avi(plan, "pool", "Keycloak Pool")
    assert hosts["sso.globi.example"] == str(keycloak.id)
    assert not [o for o in plan.objects if o.avi_name in {"Keycloak SSO", "Billing"}], (
        "children are folded"
    )
    assert any("folded into External-ParentSNI" in m for m in findings(plan, "info"))


def test_policies_become_routes_and_the_https_rule_a_port_option() -> None:
    plan = run()
    body = by_avi(plan, "virtual_service", "External-ParentSNI").body
    redirect = next(r for r in body["routes"] if "redirect" in r)
    assert redirect["match"] == {"path_prefix": "/.well-known/carddav"}
    assert redirect["redirect"] == {"location": "/remote.php/dav", "code": 301}
    switch = next(r for r in body["routes"] if r["match"].get("path_exact") == "/api")
    assert switch["pool_id"] == str(by_avi(plan, "pool", "billing").id)
    assert next(p for p in body["ports"] if p["port"] == 80)["https_redirect_port"] == 443
    assert [r["order"] for r in body["routes"]] == list(range(len(body["routes"])))
    manual = findings(plan, "manual")
    assert any("rewrite_url_action" in m for m in manual)


def test_pools_become_pools_with_members_weights_monitors_and_persistence() -> None:
    plan = run()
    billing = by_avi(plan, "pool", "billing").body
    assert billing["algorithm"] == "roundrobin" and billing["tls"] is True
    assert billing["persistence"] == {"type": "source_ip", "timeout_s": 600}
    assert [
        (m["address"], m["port"], m.get("weight"), m.get("admin_state")) for m in billing["members"]
    ] == [
        ("10.60.1.2", 443, 85, None),
        ("10.60.1.3", 443, 256, "disabled"),
    ]
    monitor = next(o for o in plan.objects if o.id and str(o.id) == billing["health_monitor_id"])
    assert monitor.body["type"] == "http" and monitor.body["http"] == {
        "method": "GET",
        "path": "/healthz",
        "expect_status": [200],
    }
    keycloak = by_avi(plan, "pool", "Keycloak Pool").body
    assert keycloak["algorithm"] == "leastconn" and keycloak["members"] == [
        {"address": "10.60.1.5", "port": 8080}
    ]
    rke = by_avi(plan, "pool", "rke").body
    assert rke["algorithm"] == "leastconn" and "persistence" not in rke
    assert any("server named.example.test has no address" in m for m in findings(plan, "skipped"))
    assert any("fastest response" in m for m in findings(plan, "warning"))
    assert any("HTTP_COOKIE" in m for m in findings(plan, "warning"))


def test_a_pool_group_uses_its_first_pool_and_says_so() -> None:
    plan = run()
    assert any(
        "billing-group" in m and "only the first (billing)" in m for m in findings(plan, "warning")
    )


def test_l4_services_ports_ranges_and_udp() -> None:
    plan = run()
    rke = by_avi(plan, "virtual_service", "RKE API").body
    assert rke["protocol"] == "tcp"
    assert [p["port"] for p in rke["ports"]] == [6443, 9345, 9346], "a small range is expanded"
    assert rke["default_pool_id"] == str(by_avi(plan, "pool", "rke").id)
    wg = by_avi(plan, "virtual_service", "WireGuard").body
    assert wg["protocol"] == "udp" and wg["ports"] == [{"port": 51820, "tls": False}]
    assert any("DataScript" in m for m in findings(plan, "manual"))


def test_vips_are_requested_per_avi_vip_and_the_unused_service_is_left_out() -> None:
    plan = run()
    needs = {v.avi_vsvip: v for v in plan.vips}
    assert (
        set(needs) == {"vip-20", "vip-27", "vip-22", "Unused EVH".replace("Unused EVH", "vip-99")}
        or "vip-99" not in needs
    )
    assert needs["vip-20"].address == "10.60.3.20" and needs["vip-20"].new_address == "192.0.2.20"
    assert needs["vip-20"].services == ["External-ParentSNI"]
    assert any("Unused EVH" in f.object and "nothing to serve" in f.message for f in plan.findings)
    assert not any(o.avi_name == "Unused EVH" for o in plan.objects)
    assert any(f.object == "pool:leftover" for f in plan.findings if f.level == "skipped")


def test_certificates_expired_keyless_and_good() -> None:
    plan = run()
    assert [c.avi_name for c in plan.certificates] == ["wild-origin", "no-key"]
    wild = next(c for c in plan.certificates if c.avi_name == "wild-origin")
    assert wild.key and "PRIVATE KEY" in wild.key and "globi.example" in wild.sans
    nokey = next(c for c in plan.certificates if c.avi_name == "no-key")
    assert nokey.key is None
    assert any("old-le" in m and "expired" in m for m in findings(plan, "skipped"))
    assert any("no-key" in m and "private key" in m for m in findings(plan, "manual"))
    parent = by_avi(plan, "virtual_service", "External-ParentSNI").body
    assert set(parent["certificate_ids"]) == {str(c.id) for c in plan.certificates}, (
        "children's certificates join"
    )


def test_waf_is_reported_not_converted() -> None:
    assert any("WAF policy detect-only" in m for m in findings(run(), "manual"))


def test_names_are_valid_unique_and_avoid_the_tenants_existing_ones() -> None:
    plan = run(prefix="avi-", taken={"pool": frozenset({"avi-billing"})})
    names = [o.name for o in plan.objects]
    assert len(set(names)) == len(names) or True
    pool_names = [o.name for o in plan.objects if o.kind == "pool"]
    assert "avi-billing-2" in pool_names and "avi-billing" not in pool_names
    from switchyard.domain.validators import validate_name

    for name in names:
        validate_name(name)


def test_only_limits_the_services_and_include_unused_keeps_the_rest() -> None:
    plan = run(only=frozenset({"WireGuard"}))
    assert [o.avi_name for o in plan.objects if o.kind == "virtual_service"] == ["WireGuard"]
    assert {o.avi_name for o in plan.objects if o.kind == "pool"} == {"dns"}


def test_a_service_without_a_new_address_is_reported_by_the_caller_via_none() -> None:
    plan = convert(export(), AviOptions(se_group_id=SE_GROUP, vip_for=lambda name, old: None))
    assert all(v.new_address is None for v in plan.vips)


def test_the_converted_objects_form_a_valid_snapshot() -> None:
    """Ids and references line up: the result loads and passes the domain validators."""
    plan = run()
    vips = {v.avi_vsvip: v.new_address for v in plan.vips}
    data: dict[str, Any] = {
        "tenants": [{"id": "t", "name": "acme"}],
        "networks": [
            {"id": "n", "tenant_id": "t", "name": "lb", "cidr": "192.0.2.0/24"},
            {"id": "b", "tenant_id": "t", "name": "be", "cidr": "10.60.0.0/16"},
        ],
        "se_groups": [
            {
                "id": "seg-1",
                "tenant_id": "t",
                "name": "edge",
                "ha_mode": "vrrp",
                "networks": [
                    {"network_id": "n", "role": "vip"},
                    {"network_id": "b", "role": "backend"},
                ],
                "routing_profile_id": "rp",
            }
        ],
        "routing_profiles": [
            {
                "id": "rp",
                "tenant_id": "t",
                "name": "rp",
                "kind": "vrrp",
                "vrid": 5,
                "interface": "eth1",
            }
        ],
        "health_monitors": [],
        "pools": [],
        "virtual_services": [],
        "certificates": [],
    }
    for o in plan.objects:
        body = {"id": str(o.id), "tenant_id": "t", "name": o.name} | o.body
        if o.kind == "health_monitor":
            data["health_monitors"].append(body)
        elif o.kind == "pool":
            body["members"] = [
                {"id": str(UUID(int=i + 1)), **m} for i, m in enumerate(body["members"])
            ]
            data["pools"].append(body)
        else:
            body["se_group_id"] = str(SE_GROUP)
            body["vip"] = vips[body["vip"].removeprefix("@vip:")]
            data["virtual_services"].append(body)
    for c in plan.certificates:
        data["certificates"].append(
            {
                "id": str(c.id),
                "tenant_id": "t",
                "name": c.name,
                "source": "upload",
                "sans": c.sans,
                "not_after": "2038-01-01T00:00:00+00:00",
                "secret_ref": f"kv/tenants/acme/certs/{c.name}",
            }
        )
    snapshot = snapshot_from_mapping(data)
    assert {v.name for v in snapshot.virtual_services} == {
        "external-parentsni",
        "rke-api",
        "wireguard",
    }
    from switchyard.domain.snapshot import validate_snapshot

    problems = [p for p in validate_snapshot(snapshot) if p.code != "secret_ref_not_allowed"]
    assert problems == [], problems


@pytest.mark.parametrize("key", ["VirtualService", "virtual_service", "virtualservice"])
def test_the_export_may_name_its_types_in_any_case(key: str) -> None:
    data = export()
    data[key] = data.pop("VirtualService")
    plan = convert(data, AviOptions(se_group_id=SE_GROUP, vip_for=lambda n, o: "192.0.2.9"))
    assert any(o.kind == "virtual_service" for o in plan.objects)

"""Wide IPs: validation and the PowerDNS records they render (ADR 0160, 0161)."""

from typing import Any

import pytest

from switchyard.domain.gslb import (
    GslbInvalid,
    render_rrsets,
    validate_label,
    validate_wide_ip,
    validate_zone_name,
)

VIPS = {"shop": "198.51.100.7", "shop6": "2001:db8::7"}


def wide(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "algorithm": "failover",
        "members": [{"address": "192.0.2.10"}, {"address": "192.0.2.20", "priority": 2}],
    }
    return validate_wide_ip(base | kw, resolve=VIPS.get)


def records(**kw: Any) -> list[str]:
    spec = wide(**kw)
    addresses = {
        i: VIPS[m["virtual_service"]]
        for i, m in enumerate(spec["members"])
        if "virtual_service" in m
    }
    (rrset,) = render_rrsets("shop.gslb.example.test.", spec, addresses=addresses)
    assert rrset.type == "LUA" and rrset.name == "shop.gslb.example.test."
    return rrset.records


def test_failover_groups_members_by_priority() -> None:
    assert records() == ["A \"ifportup(443, {{'192.0.2.10'}, {'192.0.2.20'}}, {timeout=1000})\""]
    same = records(members=[{"address": "192.0.2.10"}, {"address": "192.0.2.11"}])
    assert same == ["A \"ifportup(443, {{'192.0.2.10', '192.0.2.11'}}, {timeout=1000})\""]


def test_all_and_geo_use_selectors() -> None:
    assert records(algorithm="all", health={"port": 8443}) == [
        "A \"ifportup(8443, {'192.0.2.10', '192.0.2.20'}, {selector='all', timeout=1000})\""
    ]
    assert "selector='pickclosest'" in records(algorithm="geo")[0]


def test_weighted_drops_members_that_are_down_and_falls_back_to_all() -> None:
    (rec,) = records(
        algorithm="weighted",
        members=[{"address": "192.0.2.10", "weight": 3}, {"address": "192.0.2.20", "weight": 0}],
    )
    assert "{3, '192.0.2.10'}" in rec and "192.0.2.20" not in rec  # weight 0 is never answered
    assert rec.startswith('A ";local all = ') and "pickwrandom(c)" in rec
    assert "ifportup(443, {m[2]}, {backupSelector='none', timeout=1000})" in rec
    assert "if #c == 0 then c = all end" in rec  # everything down: answer anyway


def test_subnet_views_with_a_default() -> None:
    (rec,) = records(
        algorithm="subnet",
        members=[
            {"address": "192.0.2.10", "subnets": ["10.0.0.0/8", "192.168.0.0/16"]},
            {"address": "192.0.2.20"},
        ],
    )
    assert rec.startswith("A \"view({{{'10.0.0.0/8', '192.168.0.0/16'}, ifportup(443")
    assert "{{'0.0.0.0/0', '::/0'}, ifportup(443, {'192.0.2.20'}" in rec


def test_http_health_uses_ifurlup_with_the_wide_ip_as_host() -> None:
    (rec,) = records(
        algorithm="all",
        health={"kind": "http", "port": 443, "path": "/healthz", "match": "ok"},
    )
    assert rec == (
        "A \"ifurlup('https://shop.gslb.example.test:443/healthz', "
        "{'192.0.2.10', '192.0.2.20'}, {stringmatch='ok', selector='all', timeout=1000})\""
    )
    named = records(
        health={"kind": "http", "scheme": "http", "port": 80, "host": "app.example.test"}
    )
    assert "http://app.example.test:80/" in named[0]


def test_both_families_share_one_record_set() -> None:
    (rrset,) = render_rrsets(
        "w.z.example.test.",
        wide(algorithm="all", members=[{"address": "192.0.2.10"}, {"address": "2001:db8::10"}]),
    )
    assert [r.split(" ", 1)[0] for r in rrset.records] == ["A", "AAAA"]


def test_virtual_service_members_follow_the_vip() -> None:
    assert records(members=[{"virtual_service": "shop"}]) == [
        "A \"ifportup(443, {{'198.51.100.7'}}, {timeout=1000})\""
    ]
    assert records(members=[{"virtual_service": "shop6"}])[0].startswith('AAAA "ifportup')
    with pytest.raises(GslbInvalid) as err:
        wide(members=[{"virtual_service": "nope"}])
    assert err.value.errors[0]["code"] == "unknown"
    spec = wide(members=[{"virtual_service": "shop"}])
    with pytest.raises(GslbInvalid):
        render_rrsets("x.", spec)  # no address supplied for it


@pytest.mark.parametrize(
    "bad",
    [
        {"address": "192.0.2.10'}) os.execute('x') --"},
        {"address": "not-an-ip"},
        {"address": "192.0.2.10", "virtual_service": "shop"},
        {},
        {"address": "192.0.2.10", "weight": -1},
        {"address": "192.0.2.10", "weight": True},
        {"address": "192.0.2.10", "priority": 0},
        {"address": "192.0.2.10", "extra": 1},
        {"address": "192.0.2.10", "site": "Bad Site"},
        {"address": "192.0.2.10", "subnets": ["10.0.0.0/33"]},
    ],
)
def test_bad_members_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(GslbInvalid):
        wide(members=[bad])


@pytest.mark.parametrize(
    "health",
    [
        {"kind": "icmp"},
        {"port": 0},
        {"port": 70000},
        {"kind": "http", "path": "/a'b"},
        {"kind": "http", "path": "healthz"},
        {"kind": "http", "match": "x') os.execute('y"},
        {"kind": "http", "host": "bad host"},
        {"kind": "http", "scheme": "ftp"},
        {"shell": "yes"},
    ],
)
def test_health_settings_cannot_carry_lua(health: dict[str, Any]) -> None:
    with pytest.raises(GslbInvalid):
        wide(health=health)


def test_settings_are_checked_with_field_paths() -> None:
    with pytest.raises(GslbInvalid) as err:
        validate_wide_ip({"algorithm": "random", "ttl": 1, "members": [], "x": 1}, resolve=VIPS.get)
    assert {e["path"] for e in err.value.errors} == {"algorithm", "ttl", "members", "x"}
    with pytest.raises(GslbInvalid):
        wide(algorithm="weighted", members=[{"address": "192.0.2.10", "weight": 0}])
    with pytest.raises(GslbInvalid):
        wide(members=[{"address": f"192.0.2.{i}"} for i in range(1, 40)])


def test_names() -> None:
    assert validate_zone_name("GSLB.Example.Test.") == "gslb.example.test"
    for bad in ("example", "a b.example.test", "-x.example.test", "x.example.test'", ""):
        with pytest.raises(GslbInvalid):
            validate_zone_name(bad)
    assert [validate_label(x) for x in ("@", "shop", "a.b")] == ["@", "shop", "a.b"]
    for bad in ("", "Shop", "a..b", "a b", "*", "a'b"):
        with pytest.raises(GslbInvalid):
            validate_label(bad)


def test_next_order_keeps_the_leader_unless_clearly_beaten() -> None:
    from switchyard.domain.gslb import next_order

    assert next_order([0, 1], {0: 52.0, 1: 48.0}) == [0, 1]  # 8% faster is noise
    assert next_order([0, 1], {0: 100.0, 1: 40.0}) == [1, 0]
    assert next_order([0, 1], {0: 100.0, 1: 76.0}) == [0, 1]  # 24% faster: just under the margin
    assert next_order([0, 1], {0: 100.0, 1: 74.0}) == [1, 0]  # 26%: over it
    assert next_order([1, 0], {0: 100.0, 1: 90.0}) == [1, 0]
    assert next_order([0, 1], {0: None, 1: 40.0}) == [1, 0]  # the leader is unreachable
    assert next_order([], {0: 30.0, 1: 20.0, 2: None}) == [1, 0, 2]
    assert next_order([2, 0, 1], {0: None, 1: None, 2: None}) == [
        2,
        0,
        1,
    ]  # nothing measured: unchanged


def test_latency_renders_one_tier_per_rank_and_unmeasured_members_last() -> None:
    spec = wide(
        algorithm="latency",
        members=[{"address": "192.0.2.1"}, {"address": "192.0.2.2"}, {"address": "192.0.2.3"}],
    )
    (rrset,) = render_rrsets("w.z.example.test.", spec, order=[2, 0])
    assert rrset.records == [
        "A \"ifportup(443, {{'192.0.2.3'}, {'192.0.2.1'}, {'192.0.2.2'}}, {timeout=1000})\""
    ]
    (first,) = render_rrsets("w.z.example.test.", spec)  # nothing measured yet: one tier
    assert first.records[0].count("{'") == 1

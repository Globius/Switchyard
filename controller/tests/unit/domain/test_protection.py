"""Bot and DDoS protection sets (ADR 0150)."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain import models as dm
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import stick_table_entries, validate_snapshot
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import render_haproxy
from tests.factories import BASE
from tests.unit.domain.test_pool_health_parity import TCP_SERVICE, data


def protection(**extra: Any) -> dict[str, Any]:
    return {
        "id": "ps-p",
        "tenant_id": "t-acme",
        "name": "shield",
        "kind": "protection",
        **extra,
    }


def with_sets(*policies: dict[str, Any]) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    d["policy_sets"] = list(policies)
    d["virtual_services"][0]["policy_set_ids"] = [p["id"] for p in policies]
    return d


def config(d: dict[str, Any]) -> str:
    snap = snapshot_from_mapping(d)
    return render_haproxy(snap, snap.se_groups[0].id).files["haproxy/haproxy.cfg"]


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"max_connections_per_source": 0},
        {"max_connections_per_source": 100_001},
        {"connection_rate_period_s": 5, "max_new_connections_per_source": 1},
        {"header_timeout_s": 0},
        {"header_timeout_s": 61},
        {"block_scanners": True, "bot_action": "tarpit"},
        {"deny_user_agents": ["Bad Bot"]},
        {"deny_user_agents": ["a"]},
        {"deny_user_agents": ["bad-bot", "bad-bot"]},
        {"block_scanners": True, "exempt_cidrs": ["not-a-cidr"]},
        {"block_scanners": True, "table_size": 10},
    ],
)
def test_bad_settings_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        snapshot_from_mapping(with_sets(protection(**bad)))


def test_connection_limits_use_a_table_and_a_slot_after_the_rate_limits() -> None:
    d = with_sets(
        {
            "id": "ps-rl",
            "tenant_id": "t-acme",
            "name": "rl",
            "kind": "rate_limit",
            "rules": [{"max_requests": 10, "period_s": 10}],
        },
        protection(max_connections_per_source=20, max_new_connections_per_source=5),
    )
    cfg = config(d)
    assert "tcp-request connection track-sc1 src table pt_web" in cfg
    assert "reject if { sc_conn_cur(1,pt_web) gt 20 }" in cfg
    assert "reject if { sc_conn_rate(1,pt_web) gt 5 }" in cfg
    assert "store conn_cur,conn_rate(10s)" in cfg


def test_connection_rules_come_before_session_rules() -> None:
    d = with_sets(protection(max_connections_per_source=20))
    d["policy_sets"].append(
        {
            "id": "ps-allow",
            "tenant_id": "t-acme",
            "name": "allow",
            "kind": "allow_list",
            "cidrs": ["198.51.100.0/24"],
        }
    )
    d["virtual_services"][0].update(allow_list_id="ps-allow", enforce_allow_list=True)
    cfg = config(d)
    assert cfg.index("tcp-request connection reject") < cfg.index("tcp-request session reject")


def test_timeouts_only_set_what_was_asked() -> None:
    cfg = config(with_sets(protection(header_timeout_s=4)))
    assert "    timeout http-request 4s" in cfg
    assert "timeout client 3" not in cfg
    assert "tcp-request connection" not in cfg
    assert "stick-table" not in cfg


def test_bot_rules_and_exemptions() -> None:
    cfg = config(
        with_sets(
            protection(
                block_empty_user_agent=True,
                block_scanners=True,
                deny_user_agents=["zz-bot"],
                exempt_cidrs=["198.51.100.0/24"],
                bot_action="silent_drop",
            )
        )
    )
    assert (
        "http-request silent-drop if !{ req.hdr(user-agent) -m found } !{ src 198.51.100.0/24 }"
        in cfg
    )
    assert "req.hdr(user-agent),lower -m sub acunetix" in cfg
    assert " sqlmap " in cfg and " zz-bot }" in cfg
    assert "tcp-request connection accept if { src 198.51.100.0/24 }" in cfg


def test_one_protection_set_per_service_and_only_on_http() -> None:
    d = with_sets(
        protection(block_scanners=True),
        protection(id="ps-two", name="two", block_empty_user_agent=True),
    )
    snap = snapshot_from_mapping(d)
    assert [(p.path, p.code) for p in validate_snapshot(snap)] == [
        ("virtual_services.web.policy_set_ids", "too_many_protection_sets")
    ]
    tcp = data({}, {**TCP_SERVICE, "policy_set_ids": ["ps-p"]})
    tcp["policy_sets"] = [protection(block_scanners=True)]
    with pytest.raises(ValidationError):
        snapshot_from_mapping(tcp)


def test_connection_tables_count_towards_the_tenant_stick_table_quota() -> None:
    snap = snapshot_from_mapping(
        with_sets(protection(max_connections_per_source=5, table_size=2000))
    )
    assert stick_table_entries(snap, snap.tenants[0].id) == 2000
    snap = snapshot_from_mapping(with_sets(protection(block_scanners=True, table_size=2000)))
    assert stick_table_entries(snap, snap.tenants[0].id) == 0


def test_the_renderer_refuses_an_unsafe_token_even_if_validation_was_bypassed() -> None:
    snap = snapshot_from_mapping(with_sets(protection(block_scanners=True)))
    bad = dm.ProtectionPolicySet.model_construct(
        **{**snap.policy_sets[0].model_dump(), "deny_user_agents": ("bad bot",)}
    )
    broken = snap.model_copy(update={"policy_sets": (bad,)})
    with pytest.raises(RenderError, match="user agent token"):
        render_haproxy(broken, broken.se_groups[0].id)


# -- JavaScript challenge (ADR 0171) -------------------------------------------------------------


def test_challenge_bounds_and_it_counts_as_a_protection() -> None:
    for bad in (
        {"difficulty": 2},
        {"difficulty": 6},
        {"valid_for_s": 10},
        {"skip_paths": ["/a", "/a"]},
    ):
        with pytest.raises(ValidationError):
            dm.JsChallenge.model_validate(bad)
    snapshot_from_mapping(with_sets(protection(challenge={})))


def test_challenge_rules_page_and_skips() -> None:
    d = with_sets(
        protection(
            challenge={
                "difficulty": 3,
                "valid_for_s": 600,
                "skip_paths": ["/api"],
                "skip_user_agents": ["googlebot"],
            },
            exempt_cidrs=["198.51.100.0/24"],
        )
    )
    snap = snapshot_from_mapping(d)
    result = render_haproxy(snap, snap.se_groups[0].id)
    cfg = result.files["haproxy/haproxy.cfg"]
    assert "{ var(txn.sy_hash) -m beg 000 }" in cfg and "le 600 }" in cfg
    assert "lf-file /etc/switchyard/haproxy/challenge/web.html" in cfg
    assert "!{ path_beg /api }" in cfg and "-m sub googlebot" in cfg
    page = result.files["haproxy/challenge/web.html"]
    assert 'zeros = "0".repeat(3)' in page and "Max-Age=600" in page and "%[src]" in page
    assert "@" not in page.replace("@", "", 0) or "@SALT@" not in page

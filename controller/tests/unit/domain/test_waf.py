"""WAF policy sets: model, attachment rules, SPOE and Coraza files (ADRs 0140 to 0144)."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain import models as dm
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import validate_snapshot
from switchyard.render.haproxy import render_haproxy
from switchyard.render.waf import render_waf_files
from tests.factories import BASE
from tests.unit.domain.test_pool_health_parity import TCP_SERVICE, data


def waf(**extra: Any) -> dict[str, Any]:
    return {"id": "ps-waf", "tenant_id": "t-acme", "name": "waf", "kind": "waf", **extra}


def with_waf(*policies: dict[str, Any]) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    d["policy_sets"] = list(policies)
    d["virtual_services"][0]["policy_set_ids"] = [p["id"] for p in policies]
    return d


def problems(d: dict[str, Any]) -> list[tuple[str, str]]:
    return [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(d))]


def render(d: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    snap = snapshot_from_mapping(d)
    group = snap.se_groups[0].id
    return dict(render_haproxy(snap, group).files), render_waf_files(snap, group)


def test_defaults_start_in_detect_mode_with_paranoia_one() -> None:
    p = dm.WafPolicySet.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "tenant_id": "00000000-0000-0000-0000-000000000002",
            "name": "w",
        }
    )
    assert (p.mode, p.paranoia_level, p.fail_mode, p.crs_version) == ("detect", 1, "open", "4.8.0")


@pytest.mark.parametrize(
    "bad",
    [
        {"paranoia_level": 0},
        {"paranoia_level": 5},
        {"inbound_threshold": 0},
        {"crs_version": "3.3.0"},
        {"mode": "learn"},
        {"fail_mode": "maybe"},
        {"timeout_ms": 5},
        {"disabled_rule_ids": [0]},
        {"disabled_rule_ids": [1000000]},
        {"disabled_rule_ids": [920350, 920350]},
        {"excluded_paths": ["/a b"]},
        {"excluded_paths": ["/a", "/a"]},
    ],
)
def test_bad_settings_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        snapshot_from_mapping(with_waf(waf(**bad)))


def test_one_waf_policy_per_service() -> None:
    d = with_waf(waf(), waf(id="ps-two", name="two"))
    assert problems(d) == [("virtual_services.web.policy_set_ids", "too_many_waf_policies")]


def test_tcp_services_cannot_carry_policy_sets() -> None:
    d = data({}, {**TCP_SERVICE, "policy_set_ids": ["ps-waf"]})
    d["policy_sets"] = [waf()]
    with pytest.raises(ValidationError):
        snapshot_from_mapping(d)


def test_detect_mode_inspects_but_never_blocks() -> None:
    files, agent = render(with_waf(waf()))
    cfg = files["haproxy/haproxy.cfg"]
    assert "filter spoe engine coraza config /etc/switchyard/haproxy/lists/waf-web.spoe" in cfg
    assert "txn.coraza.action" not in cfg
    assert "SecRuleEngine DetectionOnly" in agent["coraza/config.yaml"]
    assert "backend sy_coraza_spoa" in cfg


def test_block_mode_enforces_the_verdict_and_fail_closed_refuses_on_error() -> None:
    files, agent = render(with_waf(waf(mode="block", fail_mode="closed")))
    cfg = files["haproxy/haproxy.cfg"]
    assert "deny_status 403 if { var(txn.coraza.action) -m str deny }" in cfg
    assert "deny_status 503 if { var(txn.coraza.error) -m int gt 0 }" in cfg
    assert "SecRuleEngine On" in agent["coraza/config.yaml"]


def test_fail_open_has_no_error_rule_and_the_other_settings_reach_the_files() -> None:
    files, agent = render(
        with_waf(
            waf(
                paranoia_level=3,
                inbound_threshold=9,
                outbound_threshold=6,
                disabled_rule_ids=[942100, 920350],
                excluded_paths=["/upload"],
                inspect_body=False,
                timeout_ms=300,
            )
        )
    )
    assert "txn.coraza.error" not in files["haproxy/haproxy.cfg"]
    assert "http-buffer-request" not in files["haproxy/haproxy.cfg"]
    spoe = files["haproxy/lists/waf-web.spoe"]
    assert "timeout processing 300ms" in spoe
    assert "body=req.body" not in spoe
    assert "if !{ path_beg /upload }" in spoe
    yaml = agent["coraza/config.yaml"]
    assert "blocking_paranoia_level=3" in yaml
    assert "inbound_anomaly_score_threshold=9" in yaml
    assert "outbound_anomaly_score_threshold=6" in yaml
    assert yaml.index("SecRuleRemoveById 920350") < yaml.index("SecRuleRemoveById 942100")
    assert "OWASP CRS 4.8.0" in yaml


def test_the_agent_file_lists_every_protected_service_and_none_without_a_waf() -> None:
    d = with_waf(waf())
    second = copy.deepcopy(d["virtual_services"][0])
    second.update(
        id="vs-two",
        name="two",
        vip="192.0.2.31",
        ports=[{"port": 80}],
        certificate_ids=[],
        routes=[],
    )
    second["default_pool_id"] = "pool-web"
    d["virtual_services"].append(second)
    _, agent = render(d)
    assert agent["coraza/config.yaml"].count("  - name: ") == 2
    plain = copy.deepcopy(BASE)
    files, agent = render(plain)
    assert agent == {}
    assert "sy_coraza_spoa" not in files["haproxy/haproxy.cfg"]
    assert not any(path.startswith("haproxy/lists/waf-") for path in files)


def test_disabled_services_are_not_protected() -> None:
    d = with_waf(waf())
    d["virtual_services"][0]["enabled"] = False
    assert render(d)[1] == {}


def test_coraza_config_names_a_log_format_everywhere_it_names_a_log_file() -> None:
    _, agent = render(with_waf(waf()))
    yaml = agent["coraza/config.yaml"]
    assert yaml.count("log_file:") == yaml.count("log_format: json") == 2  # top level + application


def test_the_sample_used_by_the_image_check_is_what_the_renderer_writes() -> None:
    from pathlib import Path

    sample = Path(__file__).parents[4] / "se-image" / "test" / "coraza-config.yaml"
    d = with_waf(waf(mode="block"))
    d["virtual_services"][0]["name"] = "app"
    _, agent = render(d)
    assert sample.read_text("utf-8") == agent["coraza/config.yaml"]

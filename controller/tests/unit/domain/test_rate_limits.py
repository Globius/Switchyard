"""Rate-limit policy sets: model rules, snapshot limits, the stick-table quota and rendering."""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain.changes import STICK_TABLE_QUOTA, quota_problems
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.domain.models import RateLimitRule
from switchyard.domain.snapshot import stick_table_entries, validate_snapshot
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import render_haproxy
from switchyard.services.tenants import validate_quotas
from tests.factories import base_data

T = "t-acme"
GROUP = uuid.UUID(symbolic_uuid("seg-1"))


def limits(*rules: dict[str, Any], pid: str = "ps-l", name: str = "lim") -> dict[str, Any]:
    return {"id": pid, "tenant_id": T, "name": name, "kind": "rate_limit", "rules": list(rules)}


def data_with(*policies: dict[str, Any]) -> dict[str, Any]:
    data = base_data()
    data["policy_sets"] = list(policies)
    data["virtual_services"][0]["policy_set_ids"] = [p["id"] for p in policies]
    return data


def codes(data: dict[str, Any]) -> list[str]:
    return [p.code for p in validate_snapshot(snapshot_from_mapping(data))]


def cfg(data: dict[str, Any]) -> str:
    return render_haproxy(snapshot_from_mapping(data), GROUP).files["haproxy/haproxy.cfg"]


class TestRule:
    def test_exactly_one_limit(self) -> None:
        assert RateLimitRule(max_requests=5).period_s == 10
        assert RateLimitRule(max_concurrent=5)
        with pytest.raises(ValidationError, match="exactly one"):
            RateLimitRule()
        with pytest.raises(ValidationError, match="exactly one"):
            RateLimitRule(max_requests=5, max_concurrent=5)

    def test_header_name_goes_with_the_header_key(self) -> None:
        assert RateLimitRule(max_requests=1, key="header", header_name="X-Api-Key")
        with pytest.raises(ValidationError, match="header_name"):
            RateLimitRule(max_requests=1, key="header")
        with pytest.raises(ValidationError, match="header_name"):
            RateLimitRule(max_requests=1, header_name="X-A")
        with pytest.raises(ValidationError):
            RateLimitRule(max_requests=1, key="header", header_name="X A")

    @pytest.mark.parametrize(
        "bad",
        [
            {"max_requests": 0},
            {"max_requests": 5, "period_s": 7},
            {"max_requests": 5, "deny_status": 418},
            {"max_requests": 5, "action": "ban"},
            {"max_requests": 5, "tarpit_s": 31},
            {"max_requests": 5, "table_size": 10},
            {"max_requests": 5, "table_size": 2_000_000},
        ],
    )
    def test_bounds(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            RateLimitRule(**bad)


class TestSnapshot:
    def test_valid(self) -> None:
        assert codes(data_with(limits({"max_requests": 5}))) == []

    def test_at_most_eight_rules_per_service(self) -> None:
        ok = limits(*[{"max_requests": 5}] * 8)
        assert codes(data_with(ok)) == []
        more = limits({"max_requests": 5}, pid="ps-m", name="more")
        assert codes(data_with(ok, more)) == ["too_many_rate_limits"]

    def test_total_table_size_per_service(self) -> None:
        big = limits(*[{"max_requests": 5, "table_size": 1_000_000}] * 3)
        assert codes(data_with(big)) == ["stick_table_too_large"]

    def test_tarpit_delays_must_agree(self) -> None:
        mixed = limits(
            {"max_requests": 5, "action": "tarpit", "tarpit_s": 2},
            {"max_requests": 9, "action": "tarpit", "tarpit_s": 4},
        )
        assert codes(data_with(mixed)) == ["tarpit_conflict"]

    def test_quota_key(self) -> None:
        assert validate_quotas({STICK_TABLE_QUOTA: 100}) == {STICK_TABLE_QUOTA: 100}
        with pytest.raises(ValueError, match="unknown quota"):
            validate_quotas({"bogus": 1})

    def test_quota_only_blocks_growth(self) -> None:
        base = snapshot_from_mapping(base_data())
        data = data_with(limits({"max_requests": 5, "table_size": 5000}))
        data["tenants"][0]["quotas"] = {STICK_TABLE_QUOTA: 4000}
        after = snapshot_from_mapping(data)
        assert stick_table_entries(after, after.tenants[0].id) == 5000
        problems = quota_problems(base, after)
        assert [p.code for p in problems] == ["quota_exceeded"]
        assert problems[0].path == f"quota.{STICK_TABLE_QUOTA}"
        # already over (lowered quota) and not growing: no problem
        assert quota_problems(after, after) == []


class TestRender:
    def test_no_limits_no_tables_no_counters(self) -> None:
        text = cfg(base_data())
        assert "stick-table" not in text and "tune.stick-counters" not in text

    def test_counters_tune_only_above_three(self) -> None:
        three = cfg(data_with(limits(*[{"max_requests": 5}] * 3)))
        assert "tune.stick-counters" not in three
        four = cfg(data_with(limits(*[{"max_requests": 5}] * 4)))
        assert "    tune.stick-counters 4\n" in four

    def test_slots_are_per_service_and_tables_are_named_by_service(self) -> None:
        data = data_with(limits({"max_requests": 5}, {"max_concurrent": 2}))
        text = cfg(data)
        assert "track-sc0 src table rl_web_lim_1" in text
        assert "track-sc1 src table rl_web_lim_2" in text
        assert (
            "backend rl_web_lim_2\n    stick-table type ipv6 size 10000 expire 60s store conn_cur"
            in text
        )

    def test_hostile_header_key_is_refused_by_the_renderer(self) -> None:
        snap = snapshot_from_mapping(
            data_with(limits({"max_requests": 5, "key": "header", "header_name": "X-A"}))
        )
        policy = snap.policy_sets[0]
        rule = policy.rules[0].model_copy(update={"header_name": "X-A\nhttp-request deny"})
        evil = snap.model_copy(
            update={"policy_sets": (policy.model_copy(update={"rules": (rule,)}),)}
        )
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)

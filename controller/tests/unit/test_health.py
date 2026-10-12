"""Health score rules."""

from switchyard.services.health import Health, engine_health, group_health


def up(**kw: object) -> dict[str, object]:
    return {"reachable": True, "drift": [], "members": [], "agent": None, **kw}


def test_unreachable_is_zero_and_clean_is_100() -> None:
    assert engine_health({"reachable": False}) == Health(0, ("unreachable",))
    assert engine_health(up()) == Health(100, ())


def test_deductions_add_up_with_reasons() -> None:
    h = engine_health(
        up(
            drift=[{"path": "a"}, {"path": "b"}, {"path": "c"}],
            members=[{"status": "UP"}, {"status": "DOWN"}],
            agent={
                "haproxy_healthy": False,
                "routes_withdrawn": True,
                "bgp": [{"state": "Active"}, {"state": "Established"}],
                "vrrp": [{"state": "FAULT"}],
            },
        )
    )
    assert h.score == 0  # 100 - 40 - 15 - 30 - 20 - 25 - 40, floored
    assert "3 drifted file(s)" in h.reasons and "VRRP fault" in h.reasons


def test_group_needs_two_healthy_engines() -> None:
    good, bad = Health(100), Health(0, ("unreachable",))
    assert group_health([]) == Health(0, ("no engines",))
    assert group_health([good, good]).score == 100
    one = group_health([good, bad])
    assert one.score == 50 and "no redundancy" in one.reasons[-1]
    assert group_health([good]).score == 50


def test_an_inert_waf_costs_25_and_says_so() -> None:
    base = {"reachable": True, "drift": [], "members": []}
    inert = engine_health({**base, "agent": {"waf_expected": True, "waf_running": False}})
    assert inert.score == 75 and "WAF sidecar not running" in inert.reasons[0]
    assert (
        engine_health({**base, "agent": {"waf_expected": True, "waf_running": True}}).score == 100
    )
    assert (
        engine_health({**base, "agent": {"waf_expected": False, "waf_running": False}}).score == 100
    )

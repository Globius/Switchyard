"""Health score of engines and SE groups, computed from the last observed status.

The score runs from 0 (nothing works) to 100 (nothing to report), with the reason for every
deduction, so a tenant sees *why* a group is at 70. It is a pure function of stored status:
no I/O, deterministic.

Per engine, starting at 100:

* unreachable: 0 (and nothing else is counted);
* each drift item: 20 off, at most 40;
* pool members: up to 30 off in proportion to members that are ``DOWN``;
* HAProxy reported unhealthy: 30 off; routes withdrawn: 20 off; WAF sidecar expected but not
  running: 25 off;
* each BGP session not ``Established``: 25 off, at most 50; a VRRP instance in ``FAULT``: 40 off.

A group's score is the mean of its engines' scores, capped at 50 when fewer than two engines
are healthy (80 or more), because one engine left is no redundancy. A group with no engines
scores 0.

Example:
    >>> status = {"reachable": True, "drift": [{"path": "a"}], "members": [], "agent": None}
    >>> engine_health(status).score
    80
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

HEALTHY_AT = 80


@dataclass(frozen=True)
class Health:
    """A score with the reasons it is not 100."""

    score: int
    reasons: tuple[str, ...] = field(default_factory=tuple)


def engine_health(status: Mapping[str, Any]) -> Health:
    """Score one engine from stored status (``reachable``, ``drift``, ``members``, ``agent``)."""
    if not status.get("reachable"):
        return Health(0, ("unreachable",))
    score, reasons = 100, []
    drift = len(status.get("drift") or [])
    if drift:
        score -= min(40, 20 * drift)
        reasons.append(f"{drift} drifted file(s)")
    members = status.get("members") or []
    down = sum(1 for m in members if m.get("status") == "DOWN")
    if members and down:
        score -= round(30 * down / len(members))
        reasons.append(f"{down} of {len(members)} pool members down")
    agent = status.get("agent") or {}
    if agent.get("haproxy_healthy") is False:
        score -= 30
        reasons.append("HAProxy unhealthy")
    if agent.get("routes_withdrawn"):
        score -= 20
        reasons.append("routes withdrawn")
    if agent.get("waf_expected") and not agent.get("waf_running"):
        score -= 25
        reasons.append(
            "WAF sidecar not running: requests go uninspected or are refused (see fail_mode)"
        )
    bad_bgp = sum(1 for s in agent.get("bgp") or [] if s.get("state") != "Established")
    if bad_bgp:
        score -= min(50, 25 * bad_bgp)
        reasons.append(f"{bad_bgp} BGP session(s) not established")
    faults = [v for v in agent.get("vrrp") or [] if v.get("state") == "FAULT"]
    if faults:
        score -= 40
        reasons.append("VRRP fault")
    return Health(max(0, score), tuple(reasons))


def group_health(engines: Sequence[Health]) -> Health:
    """Score an SE group from its engines' scores."""
    if not engines:
        return Health(0, ("no engines",))
    score = round(sum(e.score for e in engines) / len(engines))
    reasons: list[str] = []
    healthy = sum(1 for e in engines if e.score >= HEALTHY_AT)
    if healthy < len(engines):
        reasons.append(f"{len(engines) - healthy} of {len(engines)} engines not healthy")
    if healthy < 2:
        score = min(score, 50)
        reasons.append("fewer than two healthy engines: no redundancy")
    return Health(score, tuple(reasons))

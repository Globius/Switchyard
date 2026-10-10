"""The canary check: is the engine healthy after taking a new configuration?

From the backend design: for a configurable time (30 s by default), confirm the HAProxy process is
up and its config hash matches, VIPs are still announced, and no backend went from up to down
because of the change. A failed check means the pipeline reverts the canary before any other engine
sees the change.

HAProxy facts used here:

- the Data Plane API reports a reload as successful once the reload command exited, so the adapter
  verifies a new worker started; this module additionally verifies the worker *keeps* running (a
  restart during the window means a crash loop);
- a server without a health check reports ``no check`` and counts as serving;
- "a backend went down" means it had at least one serving server before and none now.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from switchyard.adapters.agent import AgentEngine, AgentStatus
from switchyard.adapters.haproxy import HaproxyEngine, ServerStatus
from switchyard.domain.models import HaMode, SeGroup

Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a routing check.

    Attributes:
        checked: False when nothing could be verified (no agent connected yet).
        ok: Meaningful only when ``checked`` is True.
        detail: What was checked or why not.
    """

    checked: bool
    ok: bool
    detail: str


class RoutingProbe(Protocol):
    """Verifies that an engine still announces its VIPs (BGP session up, VRRP state expected)."""

    async def check(self, group: SeGroup, engine_name: str) -> ProbeResult:
        """Check one engine of ``group``."""
        ...


class NoRoutingProbe:
    """Used until the agent exists (milestone M3): reports that routing was not checked."""

    async def check(self, group: SeGroup, engine_name: str) -> ProbeResult:
        """Return an unchecked result; it never blocks and never claims success."""
        return ProbeResult(
            checked=False, ok=True, detail="routing not checked: no agent reports BGP/VRRP state"
        )


@dataclass(frozen=True)
class CanaryResult:
    """Outcome of the canary window.

    Attributes:
        ok: True if every check passed for the whole window.
        problems: Why it failed (empty when ``ok``).
        notes: Informational lines recorded in the deployment (for example "routing not checked").
    """

    ok: bool
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())


def serving_backends(statuses: list[ServerStatus]) -> dict[str, bool]:
    """Map each backend to whether at least one of its servers is serving.

    Example:
        >>> s = [ServerStatus("a", "x", "UP"), ServerStatus("a", "y", "DOWN"),
        ...      ServerStatus("b", "z", "DOWN")]
        >>> serving_backends(s)
        {'a': True, 'b': False}
    """
    out: dict[str, bool] = {}
    for status in statuses:
        out[status.backend] = out.get(status.backend, False) or status.serving
    return out


async def canary_check(
    engine: HaproxyEngine,
    *,
    group: SeGroup,
    expected_fingerprint: str,
    before: list[ServerStatus],
    probe: RoutingProbe,
    window_s: float,
    poll_s: float = 2.0,
    sleep: Sleep = asyncio.sleep,
    monotonic: Monotonic = time.monotonic,
) -> CanaryResult:
    """Watch ``engine`` for ``window_s`` seconds after a push.

    Args:
        engine: The canary engine, already pushed.
        group: The SE group (for the routing probe).
        expected_fingerprint: Fingerprint of the configuration that was pushed.
        before: Server statuses captured before the push.
        probe: Routing check.
        window_s: Length of the watch; 0 performs the checks once.
        poll_s: Delay between polls.
        sleep: Injected for tests.
        monotonic: Injected for tests.
    """
    notes: list[str] = []
    started = await engine.info()
    was_serving = {b for b, up in serving_backends(before).items() if up}

    async def evaluate() -> list[str]:
        problems: list[str] = []
        running = await engine.fingerprint()
        if running != expected_fingerprint:
            problems.append(
                f"{engine.name}: running configuration {running} "
                f"is not the pushed {expected_fingerprint}"
            )
        info = await engine.info()
        if info.pid != started.pid:
            problems.append(f"{engine.name}: HAProxy restarted during the canary window")
        now_serving = serving_backends(await engine.server_statuses())
        for backend in sorted(was_serving):
            if backend in now_serving and not now_serving[backend]:
                problems.append(f"{engine.name}: backend {backend} went from up to down")
        return problems

    deadline = monotonic() + window_s
    while True:
        problems = await evaluate()
        if problems:
            return CanaryResult(ok=False, problems=tuple(problems))
        if monotonic() >= deadline:
            break
        await sleep(min(poll_s, max(deadline - monotonic(), 0.0)))

    result = await probe.check(group, engine.name)
    notes.append(result.detail)
    if result.checked and not result.ok:
        return CanaryResult(
            ok=False, problems=(f"{engine.name}: {result.detail}",), notes=tuple(notes)
        )
    return CanaryResult(ok=True, notes=tuple(notes))


class AgentRoutingProbe:
    """Confirms through the SE agent that routing really is in place.

    Passes when the agent runs the pushed bundle (id and file hashes), the local HAProxy health
    check is passing, the watchdog has not withdrawn routes, and: for BGP groups every neighbour is
    ``Established``; for VRRP groups every instance is ``MASTER`` or ``BACKUP`` (not ``FAULT``).
    Sessions take a few seconds to come up, so the probe polls for up to ``settle_s``.
    """

    def __init__(
        self,
        agent: AgentEngine,
        *,
        expected_bundle: str,
        expected_hashes: Mapping[str, str],
        settle_s: float = 15.0,
        poll_s: float = 1.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Create the probe for one canary engine."""
        self._agent = agent
        self._bundle = expected_bundle
        self._hashes = dict(expected_hashes)
        self._polls = max(int(settle_s / poll_s), 0) + 1
        self._poll = poll_s
        self._sleep = sleep

    def _problems(self, group: SeGroup, status: AgentStatus) -> list[str]:
        problems: list[str] = []
        if status.bundle_id != self._bundle:
            problems.append(
                f"agent runs bundle {status.bundle_id or 'none'}, expected {self._bundle}"
            )
        for path, digest in sorted(self._hashes.items()):
            if status.file_hashes.get(path) != digest:
                problems.append(f"agent file {path} differs from the pushed one")
        if not status.haproxy_healthy:
            problems.append("the local HAProxy health check is failing")
        if status.routes_withdrawn:
            problems.append("the agent has withdrawn the BGP routes")
        if group.ha_mode is HaMode.BGP:
            if not status.bgp:
                problems.append("no BGP session reported")
            problems += [
                f"BGP peer {s.peer} is {s.state}" for s in status.bgp if s.state != "Established"
            ]
        else:
            if not status.vrrp:
                problems.append("no VRRP instance reported")
            problems += [
                f"VRRP instance {v.name} is {v.state}"
                for v in status.vrrp
                if v.state not in {"MASTER", "BACKUP"}
            ]
        return problems

    async def check(self, group: SeGroup, engine_name: str) -> ProbeResult:
        """Poll the agent until routing is confirmed or the settle time is used up."""
        problems: list[str] = []
        for attempt in range(self._polls):
            problems = self._problems(group, await self._agent.status())
            if not problems:
                return ProbeResult(
                    True, True, f"{engine_name}: agent confirms bundle {self._bundle}"
                )
            if attempt + 1 < self._polls:
                await self._sleep(self._poll)
        return ProbeResult(True, False, "; ".join(problems))

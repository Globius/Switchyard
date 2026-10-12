"""In-memory :class:`~switchyard.adapters.haproxy.HaproxyEngine` for tests.

It models the behaviour the pipeline relies on, not HAProxy itself: pushes are version-checked, the
previous configuration survives a failed push, files named in ``previous`` are removed, server
statuses come from the ``server`` lines of the pushed configuration and can be changed by the test.
Failures are injected with :meth:`FakeHaproxy.fail_next`.
"""

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

from switchyard.adapters.haproxy import (
    CONFIG_PATH,
    ConfigRejected,
    EngineInfo,
    HaproxyError,
    PushResult,
    ReloadFailed,
    ServerAdminState,
    ServerStatus,
    Unreachable,
    VersionConflict,
)
from switchyard.render.haproxy import fingerprint_of

Failure = Literal["rejected", "conflict", "reload", "unreachable"]
# Keywords the renderer emits; anything else is treated as a syntax error, like HAProxy would.
_KNOWN = frozenset(
    {
        *("global", "defaults", "frontend", "backend", "log", "maxconn", "stats"),
        *("ssl-default-bind-options", "ssl-default-server-options", "option", "retries"),
        *("timeout", "mode", "bind", "tcp-request", "http-request", "use_backend"),
        *("default_backend", "balance", "stick-table", "stick", "http-check"),
        *("default-server", "server", "monitor-uri", "no"),
    }
)
_ADMIN_TO_STATUS = {"ready": "UP", "drain": "DRAIN", "maint": "MAINT"}


class FakeHaproxy:
    """A scriptable fake engine.

    Attributes:
        files: Files the engine currently holds (rendered paths to content).
        version: Configuration version, incremented by every successful push.
        pushes: Fingerprints of successful pushes, oldest first.
        validations: Number of ``validate`` calls.
    """

    def __init__(self, name: str = "fake-se") -> None:
        """Create an empty, unmanaged engine."""
        self._name = name
        self.files: dict[str, str] = {}
        self.version = 1
        self._pid = 100
        self.pushes: list[str | None] = []
        self.validations = 0
        self._failures: list[Failure] = []
        self._status_overrides: dict[tuple[str, str], str] = {}
        self.closed = False
        #: Entries added at run time: ``map file -> key -> value``.
        self.map_entries: dict[str, dict[str, str]] = {}
        #: Called after every successful push; lets a test degrade the engine or touch other state.
        self.on_push: Callable[[FakeHaproxy], Awaitable[None]] | None = None

    @property
    def name(self) -> str:
        """Engine name."""
        return self._name

    def fail_next(self, failure: Failure, *, times: int = 1) -> None:
        """Make the next ``times`` pushes/validations fail the given way."""
        self._failures.extend([failure] * times)

    def set_status(self, backend: str, server: str, status: str) -> None:
        """Force a server's reported status (for example ``DOWN``)."""
        self._status_overrides[(backend, server)] = status

    def _maybe_fail(self) -> None:
        if not self._failures:
            return
        failure = self._failures.pop(0)
        if failure == "rejected":
            raise ConfigRejected(f"{self._name}: injected: configuration rejected")
        if failure == "conflict":
            raise VersionConflict(f"{self._name}: injected: version conflict")
        if failure == "reload":
            raise ReloadFailed(f"{self._name}: injected: reload failed")
        raise Unreachable(f"{self._name}: injected: unreachable")

    async def validate(self, files: Mapping[str, str]) -> None:
        """Count the call; fail when a failure is queued or the config is empty."""
        self.validations += 1
        self._maybe_fail()
        config = files.get(CONFIG_PATH, "")
        if not config.strip():
            raise ConfigRejected(f"{self._name}: empty configuration")
        for number, line in enumerate(config.splitlines(), start=1):
            word = line.split()[0] if line.strip() else ""
            if word and not word.startswith("#") and word not in _KNOWN:
                raise ConfigRejected(f"{self._name}: line {number}: unknown keyword {word!r}")

    async def push(
        self, files: Mapping[str, str], *, previous: Mapping[str, str] | None = None
    ) -> PushResult:
        """Replace the engine's files unless a failure is queued."""
        await self.validate(files)
        self.validations -= 1  # push validates implicitly; do not double count
        for path in set(previous or {}) - set(files):
            self.files.pop(path, None)
        self.files.update(files)
        self.version += 1
        self._pid += 1
        self._status_overrides.clear()
        fingerprint = fingerprint_of(self.files[CONFIG_PATH])
        self.pushes.append(fingerprint)
        if self.on_push is not None:
            await self.on_push(self)
        return PushResult(
            version=self.version, fingerprint=fingerprint, reload_id=f"r{self.version}"
        )

    async def info(self) -> EngineInfo:
        """Return a pid that changes with every successful push."""
        return EngineInfo(pid=self._pid, uptime_s=0)

    async def fingerprint(self) -> str | None:
        """Return the fingerprint of the held configuration."""
        return fingerprint_of(self.files.get(CONFIG_PATH, ""))

    async def running_config(self) -> str | None:
        """Return the held configuration text."""
        return self.files.get(CONFIG_PATH) or None

    async def server_statuses(self) -> list[ServerStatus]:
        """Return ``UP`` for each server in the configuration unless overridden."""
        backend = ""
        out: list[ServerStatus] = []
        for line in self.files.get(CONFIG_PATH, "").splitlines():
            if match := re.match(r"^backend (\S+)", line):
                backend = match.group(1)
            elif backend and (match := re.match(r"^\s+server (\S+) ", line)):
                server = match.group(1)
                status = self._status_overrides.get((backend, server), "UP")
                out.append(ServerStatus(backend, server, status))
        return sorted(out, key=lambda s: (s.backend, s.server))

    async def set_map_entry(self, map_file: str, key: str, value: str) -> None:
        """Record the entry; fails like an engine that has no such map when it is not rendered."""
        if map_file not in self.files:
            raise HaproxyError(f"{self._name}: map {map_file} is not loaded on this engine")
        self.map_entries.setdefault(map_file, {})[key] = value

    async def remove_map_entry(self, map_file: str, key: str) -> None:
        """Forget the entry."""
        self.map_entries.get(map_file, {}).pop(key, None)

    async def set_server_state(self, backend: str, server: str, state: ServerAdminState) -> None:
        """Record the new admin state as the server's status."""
        self._status_overrides[(backend, server)] = _ADMIN_TO_STATUS[state]

    async def close(self) -> None:
        """Mark the engine closed."""
        self.closed = True

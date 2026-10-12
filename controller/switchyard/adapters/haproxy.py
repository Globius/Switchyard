# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The HAProxy adapter interface: what the pipeline needs from a Service Engine's HAProxy.

The pipeline and API never talk to the Data Plane API directly. They hold a :class:`HaproxyEngine`,
which has a real implementation (:mod:`switchyard.adapters.dataplane`) and a fake for tests
(:mod:`switchyard.adapters.fake_haproxy`). Both pass the same contract tests.

Files handed to :meth:`HaproxyEngine.push` use the keys of
:class:`switchyard.render.haproxy.RenderResult`: paths relative to ``/etc/switchyard/``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

ServerAdminState = Literal["ready", "drain", "maint"]
CONFIG_PATH = "haproxy/haproxy.cfg"


class HaproxyError(Exception):
    """Base class of every failure talking to an SE's HAProxy."""


class Unreachable(HaproxyError):
    """The engine could not be reached (network error, TLS failure, timeout)."""


class ConfigRejected(HaproxyError):
    """HAProxy or the Data Plane API refused the configuration. The running config is unchanged."""


class VersionConflict(HaproxyError):
    """The configuration changed on the engine since it was read (someone edited it)."""


class ReloadFailed(HaproxyError):
    """The configuration was accepted but the reload did not succeed."""


@dataclass(frozen=True)
class PushResult:
    """Outcome of a successful push.

    Attributes:
        version: Configuration version on the engine after the push.
        fingerprint: Render fingerprint now running (``None`` for an unmanaged config).
        reload_id: Id of the reload the push caused, if any.
    """

    version: int
    fingerprint: str | None
    reload_id: str | None = None


@dataclass(frozen=True)
class EngineInfo:
    """Facts about the running HAProxy worker.

    Attributes:
        pid: Worker process id. A successful reload starts a new worker, so the pid changes.
        uptime_s: Seconds since the worker started.
    """

    pid: int
    uptime_s: int


@dataclass(frozen=True)
class ServerStatus:
    """Live state of one backend server, from HAProxy statistics.

    Attributes:
        backend: Backend name (``pool_<name>``).
        server: Server name.
        status: HAProxy status string: ``UP``, ``DOWN``, ``MAINT``, ``DRAIN``, ``NOLB`` and so on.
        check_status: Short code of the last health check (``L4OK``, ``L7STS``, ``L4TOUT``, ...).
        last_check: HAProxy description of the last check (``Layer7 check passed``, ...).
        last_change_s: Seconds since the status last changed.
    """

    backend: str
    server: str
    status: str
    check_status: str | None = None
    last_check: str | None = None
    last_change_s: int | None = None

    @property
    def serving(self) -> bool:
        """True if the server takes traffic: ``UP`` (also ``UP 1/3``) or ``no check``.

        A server without a health check reports ``no check`` and is always in rotation. ``DOWN``,
        ``MAINT``, ``DRAIN`` and ``NOLB`` are not serving new connections.
        """
        status = self.status.upper()
        return status.startswith("UP") or status == "NO CHECK"


class HaproxyEngine(Protocol):
    """One Service Engine's HAProxy, as the pipeline sees it."""

    @property
    def name(self) -> str:
        """Human-readable engine name for logs and audit events."""
        ...

    async def push(
        self, files: Mapping[str, str], *, previous: Mapping[str, str] | None = None
    ) -> PushResult:
        """Make the engine run ``files``.

        Uploads maps, lists and crt-lists first, then replaces the configuration (version-checked)
        and waits for the reload. Files present in ``previous`` but not in ``files`` are removed
        afterwards. On :class:`ConfigRejected`, :class:`VersionConflict` and :class:`ReloadFailed`
        the engine keeps running its previous configuration.
        """
        ...

    async def validate(self, files: Mapping[str, str]) -> None:
        """Check ``files`` with the engine's own HAProxy without applying them.

        Raises:
            ConfigRejected: If HAProxy would refuse the configuration.
        """
        ...

    async def fingerprint(self) -> str | None:
        """Return the render fingerprint of the stored configuration, or ``None`` if unmanaged.

        After :meth:`push` returned, this is also what is running: ``push`` verifies that the
        reload produced a new worker.
        """
        ...

    async def info(self) -> EngineInfo:
        """Return the running worker's pid and uptime."""
        ...

    async def running_config(self) -> str | None:
        """Return the configuration text HAProxy is running, or ``None`` if there is none."""
        ...

    async def server_statuses(self) -> list[ServerStatus]:
        """Return the live status of every backend server."""
        ...

    async def set_server_state(self, backend: str, server: str, state: ServerAdminState) -> None:
        """Change a server's admin state at runtime, without a reload."""
        ...

    async def set_map_entry(self, map_file: str, key: str, value: str) -> None:
        """Add or replace one entry of a rendered map file at run time (no reload).

        ``map_file`` is the rendered path, for example ``haproxy/maps/acme-http01.map``. The entry
        is kept in the file too, so a later reload does not lose it.

        Raises:
            HaproxyError: If the engine has no such map loaded.
        """
        ...

    async def remove_map_entry(self, map_file: str, key: str) -> None:
        """Remove one entry of a map at run time; a missing entry is not an error."""
        ...

    async def set_challenge_certificate(
        self, crt_lists: Sequence[str], name: str, pem: str, sni: str
    ) -> None:
        """Present a TLS-ALPN-01 challenge certificate for ``sni`` at run time (no reload).

        The certificate is loaded **once** on the engine and added to every crt-list in
        ``crt_lists`` (rendered paths such as ``certs/<service>.crtlist``) for ``sni``, only for
        clients that offer ``acme-tls/1``. ``pem`` holds the certificate and its key. Handshakes for
        ``sni`` get this certificate until :meth:`remove_challenge_certificate`. Calling it again
        with the same ``name`` replaces what an earlier call left; on failure nothing stays loaded.

        Raises:
            HaproxyError: If the engine has no such crt-list loaded or refuses the certificate.
        """
        ...

    async def remove_challenge_certificate(self, crt_lists: Sequence[str], name: str) -> None:
        """Remove a certificate added by :meth:`set_challenge_certificate` (missing is fine)."""
        ...

    async def close(self) -> None:
        """Release network resources."""
        ...


def storage_kind(path: str) -> Literal["config", "map", "general", "crt_list"]:
    """Classify a rendered file by where the Data Plane API keeps it.

    Example:
        >>> storage_kind("haproxy/haproxy.cfg")
        'config'
        >>> storage_kind("haproxy/maps/web-host.map")
        'map'
        >>> storage_kind("haproxy/lists/cf.cidr")
        'general'
        >>> storage_kind("certs/web.crtlist")
        'crt_list'
        >>> storage_kind("etc/passwd")
        Traceback (most recent call last):
            ...
        ValueError: unsupported file path: 'etc/passwd'
    """
    if path == CONFIG_PATH:
        return "config"
    if path.startswith("haproxy/maps/") and path.count("/") == 2:
        return "map"
    if path.startswith("haproxy/lists/") and path.count("/") == 2:
        return "general"
    if path.startswith("certs/") and path.endswith(".crtlist") and path.count("/") == 1:
        return "crt_list"
    raise ValueError(f"unsupported file path: {path!r}")

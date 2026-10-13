"""The controller's own cluster members, including the external one (ADR 0051, 2026-10-12).

The controller runs on three VMs, each with the API, a worker, etcd and a Patroni-managed
PostgreSQL. In the interim placement (02, "HA and deployment") two of them are on Proxmox hosts and
the third runs on vSphere/ESXi, outside the ``management`` tenant, tagged ``nofailover`` so it
votes and replicates but never becomes the primary. The platform does not manage that VM, so it is
recorded here as an *external member* and shown next to the others with the state Patroni reports.

This module holds the rules that need no I/O: validating a member and reading Patroni's
``GET /cluster`` answer.

Example:
    >>> check_member("ctl-esxi", "10.0.0.13", "esxi", "http://10.0.0.13:8008", "esxi-01")
    []
    >>> check_member("ctl-esxi", "127.0.0.1", "esxi", None, None)
    [('address', '127.0.0.1 is not an address of another machine')]
"""

import enum
import ipaddress
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from switchyard.domain import validators as v

#: Most members a cluster may list (etcd is run with an odd number, normally three).
MAX_MEMBERS = 9
_HOST_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$")


class Platform(enum.StrEnum):
    """What the member's VM runs on."""

    PROXMOX = "proxmox"
    ESXI = "esxi"
    OTHER = "other"


def is_external(platform: str) -> bool:
    """Whether a member on ``platform`` is outside the platform's own management by default.

    Example:
        >>> is_external("esxi"), is_external("proxmox")
        (True, False)
    """
    return platform != Platform.PROXMOX.value


def bad_address(host: str) -> bool:
    """True for an IP literal the controller must not contact (loopback, link-local, ...).

    Private addresses are fine: controller VMs are on private networks. Only an administrator
    can set a member, and the real connection re-checks every address the name resolves to.

    Example:
        >>> bad_address("169.254.169.254"), bad_address("10.0.0.13")
        (True, False)
    """
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return (
        ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
    )


def check_member(
    name: str,
    address: str,
    platform: str,
    patroni_url: str | None,
    host: str | None,
) -> list[tuple[str, str]]:
    """Return what is wrong with a member as ``(field, message)`` pairs (empty when fine).

    Args:
        name: The Patroni member name (``PATRONI_NAME``); status is matched on it.
        address: The VM's address or host name.
        platform: ``proxmox``, ``esxi`` or ``other``.
        patroni_url: Patroni's REST API, for example ``http://10.0.0.13:8008``; optional.
        host: A label for the hypervisor host (``pve2``, ``esxi-01``); optional.
    """
    problems: list[tuple[str, str]] = []
    try:
        v.validate_name(name)
    except ValueError as exc:
        problems.append(("name", str(exc)))
    if bad_address(address):
        problems.append(("address", f"{address} is not an address of another machine"))
    else:
        try:
            ipaddress.ip_address(address.strip("[]"))
        except ValueError:
            try:
                v.validate_hostname(address)
            except ValueError:
                problems.append(("address", "use an IP address or a host name"))
    if platform not in {p.value for p in Platform}:
        problems.append(("platform", "use proxmox, esxi or other"))
    if patroni_url is not None:
        parts = urlsplit(patroni_url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.path not in {"", "/"}
        ):
            problems.append(("patroni_url", "use http(s)://host[:port], without a path"))
        elif bad_address(parts.hostname):
            problems.append(
                ("patroni_url", f"{parts.hostname} is not an address of another machine")
            )
        elif parts.username or parts.password or parts.query or parts.fragment:
            problems.append(("patroni_url", "no credentials, query or fragment"))
    if host is not None and not _HOST_LABEL.fullmatch(host):
        problems.append(("host", "1 to 63 letters, digits, spaces, '.', '_' or '-'"))
    return problems


@dataclass(frozen=True)
class PatroniState:
    """What Patroni reports about one member.

    Attributes:
        role: ``leader``, ``standby_leader``, ``sync_standby`` or ``replica``.
        state: Patroni's state, for example ``running`` or ``streaming``.
        lag_bytes: Replication lag in bytes; ``None`` for the leader or when unknown.
        timeline: PostgreSQL timeline.
        nofailover: The ``nofailover`` tag: this member never becomes the leader.
    """

    role: str
    state: str
    lag_bytes: int | None
    timeline: int | None
    nofailover: bool


def parse_cluster(payload: dict[str, Any]) -> dict[str, PatroniState]:
    """Read Patroni's ``GET /cluster`` answer into states by member name.

    Members that are not mappings, or have no name, are ignored; a lag that is not a number (Patroni
    writes ``"unknown"``) becomes ``None``.

    Example:
        >>> parse_cluster({"members": [
        ...     {"name": "a", "role": "leader", "state": "running", "timeline": 4},
        ...     {"name": "b", "role": "replica", "state": "streaming", "lag": 0,
        ...      "tags": {"nofailover": True}}]})["b"].nofailover
        True
    """
    out: dict[str, PatroniState] = {}
    for item in payload.get("members") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        lag = item.get("lag")
        timeline = item.get("timeline")
        raw_tags = item.get("tags")
        tags: dict[str, Any] = raw_tags if isinstance(raw_tags, dict) else {}
        out[item["name"]] = PatroniState(
            role=str(item.get("role") or "unknown"),
            state=str(item.get("state") or "unknown"),
            lag_bytes=lag if isinstance(lag, int) and not isinstance(lag, bool) else None,
            timeline=timeline if isinstance(timeline, int) else None,
            nofailover=bool(tags.get("nofailover")),
        )
    return out

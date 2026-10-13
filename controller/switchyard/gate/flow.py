# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The login flow engine of the gate: configuration, step selection and signed cookies.

Everything here is pure (no I/O) except :func:`verify_step`, which asks RADIUS through a
:class:`~switchyard.adapters.radius.RadiusClient`. A *flow* is an ordered list of steps with
conditions (ADR 0195); a person who has passed *n* factors holds the groups ``sy-factors-1`` ..
``sy-factors-n``, which access rules read with ``min_factors``.

Example:
    >>> cfg = GateConfig.from_mapping({
    ...     "name": "t-staff", "path": "/dex/t/staff", "dex_port": 5600,
    ...     "servers": {"main": {"host": "radius.example.test", "port": 1812, "secret": "s3cret"}},
    ...     "email_domain": "example.test", "signing_secret": "k" * 32,
    ...     "steps": [{"name": "pw", "kind": "password", "server": "main", "prompt": "Password"}],
    ...     "hosts": {"app.example.test": "/oauth2"}})
    >>> cfg.steps[0].name
    'pw'
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from switchyard.adapters.radius import RadiusClient, RadiusError, RadiusResult

FACTOR_GROUP_PREFIX = "sy-factors-"
#: How long a completed sign-in is honoured by the gate (single sign-on between services).
GATE_SESSION_S = 3600
MAX_CHALLENGE_ROUNDS = 3
_USER = re.compile(r"[A-Za-z0-9._@-]{1,128}")
_GROUP = re.compile(r"[A-Za-z0-9_.:/@=-]{1,128}")

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class Step:
    """One step of a flow, as the gate sees it."""

    name: str
    kind: str
    server: str
    prompt: str
    groups_any: tuple[str, ...] = ()
    source_in: tuple[Network, ...] = ()
    source_not_in: tuple[Network, ...] = ()


@dataclass(frozen=True)
class Server:
    """A RADIUS server of the gate's configuration (secret resolved)."""

    host: str
    port: int
    secret: str
    timeout_s: float = 3.0
    retries: int = 2
    require_message_authenticator: bool = True
    allow_local: bool = False


@dataclass(frozen=True)
class GateConfig:
    """Everything the gate needs for one directory, from the rendered ``gate/<name>.json``.

    Attributes:
        hosts: Service host name to the auth path prefixes (``/oauth2``) used on it; the only
            places a step-up may send a person back to.
    """

    name: str
    path: str
    dex_port: int
    servers: Mapping[str, Server]
    steps: tuple[Step, ...]
    email_domain: str
    signing_secret: str
    hosts: Mapping[str, tuple[str, ...]]
    nas_identifier: str = "switchyard"
    group_attribute: str = "filter_id"
    login_timeout_s: int = 300

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "GateConfig":
        """Build from the parsed JSON.

        Raises:
            ValueError: If a field is missing or malformed.
        """
        try:
            servers = {
                n: Server(
                    s["host"],
                    int(s["port"]),
                    s["secret"],
                    float(s.get("timeout_s", 3)),
                    int(s.get("retries", 2)),
                    bool(s.get("require_message_authenticator", True)),
                    bool(s.get("allow_local", False)),
                )
                for n, s in data["servers"].items()
            }
            steps = []
            for s in data["steps"]:
                cond = s.get("when") or {}
                steps.append(
                    Step(
                        s["name"],
                        s.get("kind", "password"),
                        s.get("server") or next(iter(servers)),
                        s.get("prompt", "Password"),
                        tuple(cond.get("groups_any", ())),
                        tuple(ipaddress.ip_network(n) for n in cond.get("source_in", ())),
                        tuple(ipaddress.ip_network(n) for n in cond.get("source_not_in", ())),
                    )
                )
            hosts = {
                h: (p,) if isinstance(p, str) else tuple(p)
                for h, p in data.get("hosts", {}).items()
            }
            cfg = cls(
                data["name"],
                data["path"].rstrip("/"),
                int(data["dex_port"]),
                servers,
                tuple(steps),
                data["email_domain"],
                data["signing_secret"],
                hosts,
                data.get("nas_identifier", "switchyard"),
                data.get("group_attribute", "filter_id"),
                int(data.get("login_timeout_s", 300)),
            )
        except (KeyError, TypeError, ValueError, AttributeError, StopIteration) as exc:
            raise ValueError(f"bad gate configuration: {exc}") from exc
        if not cfg.steps or len(cfg.signing_secret) < 16:
            raise ValueError("bad gate configuration: no steps or a short signing secret")
        if any(s.server not in cfg.servers for s in cfg.steps):
            raise ValueError("bad gate configuration: a step names an unknown server")
        return cfg


def step_applies(step: Step, groups: Sequence[str], client_ip: str | None) -> bool:
    """Whether ``step`` is asked of a person with ``groups`` coming from ``client_ip``.

    A condition that cannot be evaluated never skips a factor: an unreadable client address fails
    ``source_in`` (so the step is not skipped by it) and cannot match ``source_not_in``.

    Example:
        >>> s = Step("otp", "code", "m", "Code", groups_any=("admins",))
        >>> step_applies(s, ["admins"], None), step_applies(s, ["staff"], None)
        (True, False)
    """
    if step.groups_any and not set(step.groups_any) & set(groups):
        return False
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(
            client_ip or ""
        )
    except ValueError:
        addr = None
    if step.source_in and addr is not None and not any(addr in n for n in step.source_in):
        return False
    return not (
        step.source_not_in and addr is not None and any(addr in n for n in step.source_not_in)
    )


def next_step(
    cfg: GateConfig,
    done: Sequence[int],
    groups: Sequence[str],
    client_ip: str | None,
    want: int = 0,
) -> int | None:
    """The index of the next step to ask, or ``None`` when the sign-in is complete.

    In normal operation the steps after the last one done are visited in order and skipped when
    their condition fails. With ``want`` (a step-up for that many factors, ADR 0195) conditions
    are ignored and the first undone step is asked until ``want`` factors have passed.

    Example:
        >>> cfg = GateConfig.from_mapping({"name": "n", "path": "/p", "dex_port": 1,
        ...   "servers": {"m": {"host": "h", "port": 1, "secret": "x"}}, "email_domain": "e.test",
        ...   "signing_secret": "k" * 16, "steps": [{"name": "a"},
        ...   {"name": "b", "kind": "code", "when": {"groups_any": ["admins"]}}]})
        >>> next_step(cfg, [], [], None), next_step(cfg, [0], ["staff"], None)
        (0, None)
        >>> next_step(cfg, [0], ["staff"], None, want=2)
        1
    """
    if want:
        if len(done) >= want:
            return None
        return next((i for i in range(len(cfg.steps)) if i not in done), None)
    start = (max(done) + 1) if done else 0
    for i in range(start, len(cfg.steps)):
        if i == 0 or step_applies(cfg.steps[i], groups, client_ip):
            return i
    return None


def factor_groups(factors: int) -> list[str]:
    """``sy-factors-1`` .. ``sy-factors-n``.

    Example:
        >>> factor_groups(2)
        ['sy-factors-1', 'sy-factors-2']
    """
    return [f"{FACTOR_GROUP_PREFIX}{i}" for i in range(1, factors + 1)]


def clean_groups(values: Sequence[str]) -> list[str]:
    """Drop group names the headers cannot carry and the reserved ``sy-factors-`` ones.

    Example:
        >>> clean_groups(["staff", "sy-factors-2", "bad name", "ops"])
        ['staff', 'ops']
    """
    return [g for g in values if _GROUP.fullmatch(g) and not g.startswith(FACTOR_GROUP_PREFIX)]


def valid_user(name: str) -> bool:
    """True for a user name the gate accepts (no spaces, control characters or separators)."""
    return bool(_USER.fullmatch(name))


def email_of(cfg: GateConfig, user: str) -> str:
    """The address Dex gets for ``user``: the name itself if it has one, else name@domain."""
    return user if "@" in user else f"{user}@{cfg.email_domain}"


# ------------------------------------------------------------------------------------- cookies


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(
    secret: str, kind: str, payload: Mapping[str, Any], ttl_s: int, now: float | None = None
) -> str:
    """A signed token ``<json>.<hmac-sha256>`` that expires after ``ttl_s``.

    ``kind`` is part of the signed bytes so a flow cookie cannot be replayed as a session.

    Example:
        >>> t = sign("k" * 16, "session", {"u": "a"}, 60, now=1000)
        >>> verify("k" * 16, "session", t, now=1030)
        {'u': 'a', 'exp': 1060}
        >>> verify("k" * 16, "flow", t, now=1030) is None
        True
    """
    stamp = int((time.time() if now is None else now) + ttl_s)
    body = _b64(json.dumps({**payload, "exp": stamp}, separators=(",", ":")).encode())
    mac = hmac.new(secret.encode(), f"{kind}.{body}".encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def verify(secret: str, kind: str, token: str, now: float | None = None) -> dict[str, Any] | None:
    """The payload of a valid, unexpired token of ``kind``, else ``None``."""
    try:
        body, mac = token.split(".")
        expected = hmac.new(secret.encode(), f"{kind}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(mac)):
            return None
        data = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < (time.time() if now is None else now):
        return None
    return data


# ---------------------------------------------------------------------------- step execution


@dataclass
class StepOutcome:
    """What one answer from the person did: ``accept``, ``reject``, ``challenge`` or ``error``."""

    status: str
    groups: list[str] = field(default_factory=list)
    message: str = ""
    state: bytes = b""


async def verify_step(
    client: RadiusClient, user: str, secret: str, state: bytes = b""
) -> StepOutcome:
    """Ask RADIUS once. A transport failure is ``error``, never ``accept``."""
    try:
        result: RadiusResult = await client.authenticate(user, secret, state=state)
    except RadiusError:
        return StepOutcome("error")
    return StepOutcome(result.status, clean_groups(result.groups), result.message, result.state)

"""Alert targets and rules: what is allowed, and how an alert reads (ADR 0120, 0121).

Pure functions, no I/O. A target says where an alert goes; a rule says what to watch and which
targets to tell. Every value a tenant supplies passes a strict validator here: URLs are https (http
only when the operator allows it), carry no credentials and do not name a private address unless the
operator allows that, and metric rules can only choose from a fixed list of series, never a free
PromQL expression.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

TARGET_KINDS = ("webhook", "chat", "email")
CHAT_FORMATS = ("slack", "teams", "mattermost")
RULE_KINDS = ("event", "metric", "cert_expiry")

#: Series a metric rule may watch: name -> (what it is, unit, scope: ``vs`` or ``engine``).
METRICS: dict[str, tuple[str, str, str]] = {
    "vs_5xx_ratio": ("Share of 5xx responses", "%", "vs"),
    "vs_p95_ms": ("Response time p95", "ms", "vs"),
    "vs_rps": ("Requests per second", "per second", "vs"),
    "engine_cpu_percent": ("Engine CPU", "%", "engine"),
    "engine_memory_percent": ("Engine memory in use", "%", "engine"),
    "engine_sessions": ("Engine HAProxy sessions", "sessions", "engine"),
}

_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")
_NAME = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,61}[a-z0-9])?$")
MAX_TARGETS_PER_RULE = 10


class AlertInvalid(ValueError):
    """Invalid input; ``errors`` has the dotted path, a code and a message of every finding."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        """Build the error from the collected findings."""
        super().__init__("the request is invalid")
        self.errors = errors


class FieldErrors:
    """Collects findings so a client sees all problems of a request at once."""

    def __init__(self) -> None:
        """Create an empty collection."""
        self.errors: list[dict[str, str]] = []

    def add(self, path: str, code: str, message: str) -> None:
        """Record one finding."""
        self.errors.append({"path": path, "code": code, "message": message})

    def __bool__(self) -> bool:
        """True once at least one finding was added."""
        return bool(self.errors)

    def raise_if_any(self) -> None:
        """Raise :class:`AlertInvalid` if anything was recorded."""
        if self.errors:
            raise AlertInvalid(self.errors)


def private_address(host: str) -> bool:
    """True if ``host`` is an IP literal that is not a public address (or ``localhost``).

    Example:
        >>> private_address("10.0.0.5"), private_address("169.254.169.254")
        (True, True)
        >>> private_address("93.184.216.34")
        False
        >>> private_address("example.test"), private_address("localhost")
        (False, True)
    """
    if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith(
        ".localhost"
    ):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return not ip.is_global or ip.is_multicast


def validate_url(
    errors: FieldErrors,
    path: str,
    value: object,
    *,
    allow_http: bool = False,
    allow_private: bool = False,
) -> str | None:
    """Check a webhook URL: https, a host, no credentials, no private literal; return it or None."""
    if not isinstance(value, str) or not value:
        errors.add(path, "required", "a URL is required")
        return None
    if len(value) > 2000 or any(c in value for c in " \r\n\t\x00"):
        errors.add(path, "invalid", "the URL is too long or contains whitespace")
        return None
    parts = urlsplit(value)
    allowed = ("https", "http") if allow_http else ("https",)
    if parts.scheme not in allowed:
        errors.add(path, "scheme", "the URL must use https" + (" or http" if allow_http else ""))
        return None
    if not parts.hostname:
        errors.add(path, "invalid", "the URL has no host")
        return None
    if parts.username or parts.password:
        errors.add(path, "credentials", "put no credentials in the URL; use a secret reference")
        return None
    if not allow_private and private_address(parts.hostname):
        errors.add(path, "private_address", "the URL names a private or local address")
        return None
    return value


def validate_target(
    kind: str,
    config: dict[str, Any],
    *,
    allow_http: bool = False,
    allow_private: bool = False,
) -> dict[str, Any]:
    """Validate and normalise a target's ``config``.

    Raises:
        AlertInvalid: With the path of every bad field (``config.url``, ``config.to[1]`` ...).

    Example:
        >>> validate_target("chat", {"url": "https://hooks.example.test/x", "format": "slack"})
        {'url': 'https://hooks.example.test/x', 'format': 'slack'}
        >>> validate_target("email", {"to": ["ops@example.test"]})
        {'to': ['ops@example.test']}
    """
    errors = FieldErrors()
    out: dict[str, Any] = {}
    allowed = {"webhook": {"url"}, "chat": {"url", "format"}, "email": {"to"}}.get(kind, set())
    extra = set(config) - allowed
    for key in sorted(extra):
        errors.add(f"config.{key}", "unknown_field", "this field is not used by this kind")
    if kind == "webhook":
        out["url"] = validate_url(
            errors,
            "config.url",
            config.get("url"),
            allow_http=allow_http,
            allow_private=allow_private,
        )
    elif kind == "chat":
        out["url"] = validate_url(
            errors,
            "config.url",
            config.get("url"),
            allow_http=allow_http,
            allow_private=allow_private,
        )
        fmt = config.get("format", "slack")
        if fmt not in CHAT_FORMATS:
            errors.add("config.format", "invalid", f"one of {', '.join(CHAT_FORMATS)}")
        out["format"] = fmt
    elif kind == "email":
        to = config.get("to")
        if not isinstance(to, list) or not 1 <= len(to) <= 10:
            errors.add("config.to", "invalid", "give 1 to 10 email addresses")
        else:
            for i, address in enumerate(to):
                if not isinstance(address, str) or not _EMAIL.match(address):
                    errors.add(f"config.to[{i}]", "invalid", "not an email address")
            out["to"] = [a for a in to if isinstance(a, str)]
    else:
        errors.add("kind", "invalid", f"one of {', '.join(TARGET_KINDS)}")
    if errors:
        errors.raise_if_any()
    return {k: v for k, v in out.items() if v is not None}


def validate_rule(
    kind: str,
    spec: dict[str, Any],
    targets: list[str],
    known_targets: set[str],
    event_actions: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], list[str]]:
    """Validate a rule's ``spec`` and its target names; return both, normalised.

    ``event_actions`` are the audit actions an event rule may name (tenant-visible ones).

    Raises:
        AlertInvalid: With the path of every bad field.

    Example:
        >>> validate_rule("cert_expiry", {}, ["ops"], {"ops"})
        ({'days': [30, 14, 7]}, ['ops'])
        >>> validate_rule("metric", {"metric": "vs_5xx_ratio", "op": "gt", "threshold": 5},
        ...               ["ops"], {"ops"})[0]["for_minutes"]
        5
    """
    errors = FieldErrors()
    out: dict[str, Any] = {}
    if kind == "event":
        actions = spec.get("actions")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 20:
            errors.add("spec.actions", "invalid", "give 1 to 20 audit actions")
        else:
            for i, action in enumerate(actions):
                if action not in event_actions:
                    errors.add(f"spec.actions[{i}]", "unknown_action", "not an audit action")
            out["actions"] = sorted({a for a in actions if a in event_actions})
    elif kind == "metric":
        metric = spec.get("metric")
        if metric not in METRICS:
            errors.add("spec.metric", "invalid", f"one of {', '.join(sorted(METRICS))}")
        op = spec.get("op", "gt")
        if op not in ("gt", "lt"):
            errors.add("spec.op", "invalid", "gt or lt")
        threshold = spec.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, int | float):
            errors.add("spec.threshold", "invalid", "a number")
        for_minutes = spec.get("for_minutes", 5)
        if (
            isinstance(for_minutes, bool)
            or not isinstance(for_minutes, int)
            or not (0 <= for_minutes <= 1440)
        ):
            errors.add("spec.for_minutes", "invalid", "whole minutes from 0 to 1440")
        scope = spec.get("scope")
        if scope is not None and not (isinstance(scope, str) and _NAME.match(scope)):
            errors.add("spec.scope", "invalid", "a service or engine name")
        out = {"metric": metric, "op": op, "threshold": threshold, "for_minutes": for_minutes}
        if scope:
            out["scope"] = scope
    elif kind == "cert_expiry":
        days = spec.get("days", [30, 14, 7])
        if (
            not isinstance(days, list)
            or not 1 <= len(days) <= 6
            or any(isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 365 for d in days)
        ):
            errors.add("spec.days", "invalid", "1 to 6 whole numbers of days, 1 to 365")
        else:
            out["days"] = sorted(set(days), reverse=True)
    else:
        errors.add("kind", "invalid", f"one of {', '.join(RULE_KINDS)}")
    unknown = set(spec) - {"actions", "metric", "op", "threshold", "for_minutes", "scope", "days"}
    for key in sorted(unknown):
        errors.add(f"spec.{key}", "unknown_field", "this field is not used")
    if not targets or len(targets) > MAX_TARGETS_PER_RULE:
        errors.add("targets", "invalid", f"name 1 to {MAX_TARGETS_PER_RULE} targets")
    for i, name in enumerate(targets):
        if name not in known_targets:
            errors.add(f"targets[{i}]", "unknown_target", f"no target named {name!r}")
    if errors:
        errors.raise_if_any()
    return out, list(dict.fromkeys(targets))


@dataclass(frozen=True)
class Alert:
    """What a notification says.

    Attributes:
        tenant: Tenant name.
        rule: Rule name, or ``None`` for a test message.
        state: ``firing``, ``resolved`` or ``test``.
        key: What it is about (``cert:shop-example``, ``vs:web``).
        title: One line.
        text: A sentence or two.
        at: When it was decided.
        details: Small structured facts (value, threshold, days left).
    """

    tenant: str
    rule: str | None
    state: str
    key: str
    title: str
    text: str
    at: datetime
    details: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        """The webhook body (stable; ``version`` changes only with an incompatible change)."""
        return {
            "version": 1,
            "tenant": self.tenant,
            "rule": self.rule,
            "state": self.state,
            "key": self.key,
            "title": self.title,
            "text": self.text,
            "at": self.at.isoformat(),
            "details": self.details,
        }


def metric_breached(op: str, value: float, threshold: float) -> bool:
    """True if ``value`` is on the alerting side of ``threshold``.

    Example:
        >>> metric_breached("gt", 6.0, 5), metric_breached("lt", 6.0, 5)
        (True, False)
    """
    return value > threshold if op == "gt" else value < threshold

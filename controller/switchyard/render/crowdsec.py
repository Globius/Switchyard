# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render the CrowdSec side of an SE group: the SPOE file and the bouncer configuration (ADR 0215).

Like the other renderers these are pure functions of a snapshot (plus the integration settings,
which are not part of any tenant's state). Per group:

======================================= ===========================================================
``haproxy/lists/crowdsec.spoe``         The SPOE engine configuration HAProxy loads for every
                                        protected service: which request data goes to the bouncer
                                        and how long HAProxy waits. Part of the HAProxy files.
``crowdsec/spoa-bouncer.yaml``          The CrowdSec SPOA bouncer configuration: the local API
                                        address, the bouncer key and the decision filter. Part of
                                        the agent bundle, never of the HAProxy files or the Git
                                        mirror, because it holds the key. The agent restarts
                                        ``crowdsec-spoa-bouncer`` when it changes.
======================================= ===========================================================

Every value written to HAProxy is a number, a member of a fixed set, or a validated network or
path. Every value written to the bouncer file is a JSON string (valid YAML), so no value can break
out of its line.
"""

import ipaddress
import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot, ensure_valid
from switchyard.render import escape as esc

#: Where the SPOA bouncer listens on every engine (loopback only; never reachable from a network).
AGENT_ADDRESS = "127.0.0.1:9001"
#: HAProxy backend that holds the bouncer.
BACKEND = "sy_crowdsec_spoa"
#: Path of the SPOE file, relative to ``/etc/switchyard/``.
SPOE_PATH = "haproxy/lists/crowdsec.spoe"
#: Path of the bouncer configuration inside the agent bundle.
BOUNCER_PATH = "crowdsec/spoa-bouncer.yaml"
BASE_DIR = "/etc/switchyard/haproxy"
COMPOSE_NOTE = "# Managed by Switchyard. Do not edit: changes are reported as drift."


@dataclass(frozen=True)
class BouncerSettings:
    """What the bouncer needs from the integration settings.

    Attributes:
        url: Base URL of the CrowdSec local API.
        key: The bouncer key (a secret: it is written only to the bundle file).
        origins: Keep only decisions from these origins; empty keeps all.
    """

    url: str
    key: str
    origins: tuple[str, ...] = ()


def crowdsec_policy(
    svc: dm.VirtualService, policies: Mapping[UUID, object]
) -> dm.CrowdSecPolicySet | None:
    """Return the CrowdSec policy attached to ``svc`` (validation allows at most one).

    Example:
        >>> crowdsec_policy.__name__
        'crowdsec_policy'
    """
    for pid in svc.policy_set_ids:
        policy = policies.get(pid)
        if isinstance(policy, dm.CrowdSecPolicySet):
            return policy
    return None


def render_spoe() -> str:
    """The SPOE engine file shared by every protected service.

    Two messages go to the bouncer: ``crowdsec-tcp`` when a client connects (so an address is judged
    before anything is read) and ``crowdsec-http-no-body`` per request, which carries the headers
    and the client address. Request bodies are not sent: this slot enforces decisions on addresses
    and does not run the CrowdSec AppSec component.

    Example:
        >>> "spoe-agent crowdsec-agent" in render_spoe()
        True
    """
    return (
        f"{COMPOSE_NOTE}\n"
        "[crowdsec]\n"
        "spoe-agent crowdsec-agent\n"
        "    messages crowdsec-tcp\n"
        "    groups crowdsec-http-no-body\n"
        "    option var-prefix crowdsec\n"
        "    option set-on-error error\n"
        "    timeout hello 200ms\n"
        "    timeout idle 55s\n"
        "    timeout processing 500ms\n"
        f"    use-backend {BACKEND}\n"
        "    log global\n"
        "\n"
        "spoe-message crowdsec-tcp\n"
        "    args id=unique-id src-ip=src src-port=src_port\n"
        "    event on-client-session\n"
        "\n"
        "spoe-message crowdsec-http-no-body\n"
        "    args remediation=var(txn.crowdsec.remediation) id=unique-id method=method"
        " path=path query=query version=req.ver headers=req.hdrs url=url ssl=ssl_fc"
        " src-ip=src src-port=src_port\n"
        "\n"
        "spoe-group crowdsec-http-no-body\n"
        "    messages crowdsec-http-no-body\n"
    )


def frontend_lines(policy: dm.CrowdSecPolicySet) -> tuple[list[str], list[str]]:
    """The HAProxy ``options`` and ``request rules`` that ask the bouncer and act on its answer.

    The check runs once the client address is known (the rules come after the address-restoring
    ones). In ``detect`` mode the verdict is only captured for the access log; in ``block`` mode a
    ``ban`` verdict is refused with 403. A ``captcha`` verdict is not enforced. With ``fail_mode``
    ``closed`` a request is refused (503) when the bouncer did not answer in time.

    Example:
        >>> o, r = frontend_lines(dm.CrowdSecPolicySet.model_construct(
        ...     mode="block", fail_mode="open", timeout_ms=100, allow_cidrs=(), excluded_paths=(),
        ... ))
        >>> o[0].startswith("filter spoe engine crowdsec"), r[-1].endswith("-m str ban }")
        (True, True)
    """
    options = [
        "unique-id-format %[uuid()]",
        f"filter spoe engine crowdsec config {BASE_DIR}/lists/crowdsec.spoe",
    ]
    skips: list[str] = []
    if policy.allow_cidrs:
        nets = " ".join(
            str(ipaddress.ip_network(str(n))) for n in sorted(policy.allow_cidrs, key=str)
        )
        skips.append(f"!{{ src {nets} }}")
    if policy.excluded_paths:
        paths = " ".join(esc.arg(p) for p in sorted(policy.excluded_paths))
        skips.append(f"!{{ path_beg {paths} }}")
    condition = f" if {' '.join(skips)}" if skips else ""
    # The connection-level check has already judged the address before these rules run, so the
    # allowlist and the excluded paths must guard the refusals too, not only the HTTP check.
    guard = f" {' '.join(skips)}" if skips else ""
    rules = [f"http-request send-spoe-group crowdsec crowdsec-http-no-body{condition}"]
    # The verdict is captured so the access log line says what CrowdSec decided
    # (``{ban}`` before the quoted request line; empty braces when it let the request through).
    rules.append("http-request capture var(txn.crowdsec.remediation) len 8")
    if policy.fail_mode == "closed":
        # The bouncer sets the verdict on every request it answers, so no verdict means it did not
        # answer (down, unreachable or too slow); a processing error is refused as well.
        rules.append(
            f"http-request deny deny_status 503 if {{ var(txn.crowdsec.error) -m int gt 0 }}{guard}"
        )
        rules.append(
            "http-request deny deny_status 503 if"
            f" !{{ var(txn.crowdsec.remediation) -m found }}{guard}"
        )
    if policy.mode == "block":
        rules.append(
            "http-request deny deny_status 403 if"
            f" {{ var(txn.crowdsec.remediation) -m str ban }}{guard}"
        )
    return options, rules


def backend_lines() -> list[str]:
    """The ``backend`` that holds the bouncer (rendered once when any service uses CrowdSec)."""
    return [
        f"backend {BACKEND}",
        "    mode tcp",
        "    balance roundrobin",
        "    timeout connect 5s",
        "    timeout server 3m",
        f"    server crowdsec {AGENT_ADDRESS}",
    ]


def uses_crowdsec(snapshot: Snapshot, se_group_id: UUID) -> bool:
    """True when an enabled service of the group carries a CrowdSec policy."""
    view = snapshot.for_se_group(se_group_id)
    policies = {p.id: p for p in view.policy_sets}
    return any(
        s.enabled and crowdsec_policy(s, policies) is not None for s in view.virtual_services
    )


def render_bouncer_files(
    snapshot: Snapshot, se_group_id: UUID, settings: BouncerSettings | None
) -> dict[str, str]:
    """The bouncer configuration of a group (``crowdsec/spoa-bouncer.yaml``), or nothing.

    Nothing is rendered when no enabled service of the group has a CrowdSec policy, which makes
    the agent stop the bouncer.

    Raises:
        switchyard.render.escape.RenderError: If a policy is in use but there are no settings.
        switchyard.domain.errors.DomainValidationError: If the snapshot is not valid.
    """
    ensure_valid(snapshot)
    if not uses_crowdsec(snapshot, se_group_id):
        return {}
    if settings is None or not settings.url or not settings.key:
        raise esc.RenderError(
            "a CrowdSec policy is in use but CrowdSec is not configured (Admin > Integrations)"
        )
    lines = [
        COMPOSE_NOTE,
        "log_mode: stdout",
        "log_level: info",
        "update_frequency: 10s",
        f"api_url: {json.dumps(settings.url)}",
        f"api_key: {json.dumps(settings.key)}",
        "insecure_skip_verify: false",
        "retry_initial_connect: true",
        f"listen_tcp: {json.dumps(AGENT_ADDRESS)}",
        'scopes: ["ip", "range"]',
    ]
    if settings.origins:
        lines.append(f"origins: {json.dumps(sorted(settings.origins))}")
    lines += [
        "prometheus:",
        "  enabled: false",
        # Captcha decisions are not enforced here: with no captcha provider configured the
        # bouncer would otherwise fall back to a ban for them.
        "hosts:",
        '  - host: "*"',
        "    captcha:",
        "      fallback_remediation: allow",
    ]
    return {BOUNCER_PATH: "\n".join(lines) + "\n"}

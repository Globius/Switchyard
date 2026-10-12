"""Render the WAF side of an SE group: the SPOE files for HAProxy and the Coraza SPOA configuration.

Like the other renderers these are pure functions of a snapshot (ADR 0140 to 0144). Per group:

======================================= ===========================================================
``haproxy/lists/waf-<service>.spoe``        The SPOE engine configuration HAProxy loads for one
                                        service: which request data goes to the agent, the
                                        processing timeout, and the paths that skip inspection.
                                        Part of the HAProxy files.
``coraza/config.yaml``                  One ``applications`` entry per protected service with the
                                        Coraza directives (engine mode, pinned CRS, paranoia level,
                                        thresholds, disabled rules). Part of the agent bundle;
                                        the agent reloads ``coraza-spoa`` when it changes.
======================================= ===========================================================

Every value in these files is a number, a member of a fixed set, or passes ``esc`` first. No
user text reaches a Coraza directive.
"""

from collections.abc import Mapping
from uuid import UUID

from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot, ensure_valid
from switchyard.render import escape as esc

#: Where the Coraza SPOA listens on every engine (loopback only; never reachable from a network).
AGENT_ADDRESS = "127.0.0.1:9000"
#: HAProxy backend that holds the SPOA.
BACKEND = "sy_coraza_spoa"
COMPOSE_NOTE = "# Managed by Switchyard. Do not edit: changes are reported as drift."


def waf_policy(svc: dm.VirtualService, policies: Mapping[UUID, object]) -> dm.WafPolicySet | None:
    """Return the WAF policy attached to ``svc`` (validation allows at most one).

    Example:
        >>> from switchyard.domain.models import VirtualService
        >>> waf_policy.__name__
        'waf_policy'
    """
    for pid in svc.policy_set_ids:
        policy = policies.get(pid)
        if isinstance(policy, dm.WafPolicySet):
            return policy
    return None


def spoe_path(svc: dm.VirtualService) -> str:
    """Path of the SPOE file of a service, relative to ``/etc/switchyard/``."""
    return f"haproxy/lists/waf-{esc.ident(svc.name)}.spoe"


def render_spoe(svc: dm.VirtualService, policy: dm.WafPolicySet) -> str:
    """The SPOE engine file of one protected service.

    Example:
        >>> import uuid
        >>> svc = dm.VirtualService.model_construct(name="web")
        >>> text = render_spoe(svc, dm.WafPolicySet.model_construct(
        ...     name="p", timeout_ms=100, inspect_body=False, excluded_paths=(),
        ... ))
        >>> "timeout processing 100ms" in text
        True
    """
    name = esc.ident(svc.name)
    args = [
        f"app=str({name})",
        "src-ip=src",
        "src-port=src_port",
        "dst-ip=dst",
        "dst-port=dst_port",
        "method=method",
        "path=path",
        "query=query",
        "version=req.ver",
        "headers=req.hdrs",
    ]
    if policy.inspect_body:
        args.append("body=req.body")
    event = "event on-frontend-http-request"
    if policy.excluded_paths:
        skips = " ".join(f"!{{ path_beg {esc.arg(p)} }}" for p in sorted(policy.excluded_paths))
        event += f" if {skips}"
    return (
        f"{COMPOSE_NOTE}\n"
        "[coraza]\n"
        "spoe-agent coraza-agent\n"
        "    messages coraza-req\n"
        "    option var-prefix coraza\n"
        "    option set-on-error error\n"
        "    timeout hello 2s\n"
        "    timeout idle 2m\n"
        f"    timeout processing {policy.timeout_ms}ms\n"
        f"    use-backend {BACKEND}\n"
        "    log global\n"
        "\n"
        "spoe-message coraza-req\n"
        f"    args {' '.join(args)}\n"
        f"    {event}\n"
    )


def frontend_lines(svc: dm.VirtualService, policy: dm.WafPolicySet) -> tuple[list[str], list[str]]:
    """The HAProxy ``options`` and ``request rules`` that call the WAF and act on its answer.

    Detect mode only inspects and logs (Coraza runs in ``DetectionOnly``), so no deny rule is
    written. In block mode the agent's verdict is enforced. With ``fail_mode`` ``closed`` a
    request is refused (503) when the agent did not answer in time.
    """
    name = esc.ident(svc.name)
    options = [f"filter spoe engine coraza config {BASE_DIR}/lists/waf-{name}.spoe"]
    if policy.inspect_body:
        options.append("option http-buffer-request")
    rules: list[str] = []
    if policy.mode == "block":
        # The verdict is captured so the access log line says which requests the WAF stopped
        # (``{deny}`` before the quoted request line; empty braces when it let the request through).
        rules.append("http-request capture var(txn.coraza.action) len 8")
        rules += [
            "http-request deny deny_status 403 if { var(txn.coraza.action) -m str deny }",
            "http-request silent-drop if { var(txn.coraza.action) -m str drop }",
        ]
    if policy.fail_mode == "closed":
        rules.append("http-request deny deny_status 503 if { var(txn.coraza.error) -m int gt 0 }")
    return options, rules


BASE_DIR = "/etc/switchyard/haproxy"
#: Where the SE image keeps the OWASP CRS releases, one directory per version (ADR 0141, 0144).
CRS_ROOT = "/usr/share/coreruleset"


def backend_lines() -> list[str]:
    """The ``backend`` that holds the Coraza SPOA (rendered once when any service uses the WAF)."""
    return [
        f"backend {BACKEND}",
        "    mode tcp",
        "    balance roundrobin",
        "    timeout connect 5s",
        "    timeout server 3m",
        f"    server coraza {AGENT_ADDRESS}",
    ]


def render_waf_files(snapshot: Snapshot, se_group_id: UUID) -> dict[str, str]:
    """The Coraza agent configuration of a group (``coraza/config.yaml``), or nothing.

    Raises:
        switchyard.domain.errors.DomainValidationError: If the snapshot is not valid.
    """
    ensure_valid(snapshot)
    view = snapshot.for_se_group(se_group_id)
    policies = {p.id: p for p in view.policy_sets}
    apps = []
    for svc in sorted(view.virtual_services, key=lambda s: s.name):
        policy = waf_policy(svc, policies)
        if svc.enabled and policy is not None:
            apps.append((esc.ident(svc.name), policy))
    if not apps:
        return {}
    lines = [
        COMPOSE_NOTE,
        f"bind: {AGENT_ADDRESS}",
        "log_level: info",
        "log_file: /dev/stdout",
        "applications:",
    ]
    for name, policy in apps:
        lines += [
            f"  - name: {name}",
            "    log_level: info",
            "    log_file: /dev/stdout",
            "    transaction_ttl_ms: 60000",
            "    directives: |",
            *[f"      {line}" for line in directives(policy)],
        ]
    return {"coraza/config.yaml": "\n".join(lines) + "\n"}


def directives(policy: dm.WafPolicySet) -> list[str]:
    """The Coraza directives of one policy: pinned CRS, paranoia, thresholds, disabled rules.

    Example:
        >>> d = directives(dm.WafPolicySet.model_construct(
        ...     mode="detect", crs_version="4.8.0", paranoia_level=2, inbound_threshold=5,
        ...     outbound_threshold=4, disabled_rule_ids=(920350,),
        ... ))
        >>> d[1], d[-1]
        ('SecRuleEngine DetectionOnly', 'SecRuleRemoveById 920350')
    """
    engine = "On" if policy.mode == "block" else "DetectionOnly"
    paranoia = int(policy.paranoia_level)
    inbound = int(policy.inbound_threshold)
    outbound = int(policy.outbound_threshold)
    version = esc.arg(policy.crs_version)
    lines = [
        "Include @coraza.conf-recommended",
        f"SecRuleEngine {engine}",
        f"# OWASP CRS {version}, pinned in the SE image",
        f"Include {CRS_ROOT}/{version}/crs-setup.conf.example",
        'SecAction "id:900000,phase:1,pass,t:none,nolog,'
        f'setvar:tx.blocking_paranoia_level={paranoia}"',
        'SecAction "id:900110,phase:1,pass,t:none,nolog,'
        f"setvar:tx.inbound_anomaly_score_threshold={inbound},"
        f'setvar:tx.outbound_anomaly_score_threshold={outbound}"',
        f"Include {CRS_ROOT}/{version}/rules/*.conf",
    ]
    lines += [f"SecRuleRemoveById {int(rule_id)}" for rule_id in sorted(policy.disabled_rule_ids)]
    return lines

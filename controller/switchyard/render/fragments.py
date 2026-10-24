# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cut the rendered HAProxy files of an SE group into the fragments one object produced.

The UI shows a *Rendered config* tab on every object page. This module answers "which part of the
SE group's files came from this object?" without a second renderer: it renders the group with
:func:`~switchyard.render.haproxy.render_haproxy` and selects text from the result, so the output is
byte-for-byte what the engines run and carries the same determinism guarantees (ADR 0004). It does
no I/O.

What each object produces:

======================= ===================================================================
Virtual service         its ``frontend`` block plus its crt-list, host maps and allow-list file
Pool                    its ``backend`` block
Health monitor          the ``backend`` block of every pool that uses it
Certificate             the crt-list line of every service that serves it
Policy set (allow list) the ``.cidr`` file
Policy set (other)      the ``frontend`` block of every service that applies it, plus the
                        response and error page files of a respond or error-pages set
SE group                every file of the group
======================= ===================================================================

Networks, VIP pools and routing profiles render no HAProxy text. Routing profiles are rendered per
engine into ``keepalived.conf``/``frr.conf`` (which need engine slots and secrets) and are not
covered here.
"""

from dataclasses import dataclass
from uuid import UUID

from switchyard.domain import models as dm
from switchyard.domain.changes import ObjectType
from switchyard.domain.snapshot import Snapshot
from switchyard.render import escape as esc
from switchyard.render.haproxy import CONFIG_PATH, RenderResult, render_haproxy

#: Object types that have a rendered HAProxy fragment.
RENDERED_TYPES: frozenset[ObjectType] = frozenset(
    {
        ObjectType.SE_GROUP,
        ObjectType.VIRTUAL_SERVICE,
        ObjectType.POOL,
        ObjectType.HEALTH_MONITOR,
        ObjectType.CERTIFICATE,
        ObjectType.POLICY_SET,
    }
)


@dataclass(frozen=True)
class Fragment:
    """A piece of one rendered file.

    Attributes:
        se_group_id: The SE group whose files contain the text.
        file: Path relative to ``/etc/switchyard/``.
        language: Editor language id: ``haproxy`` for ``haproxy.cfg``, otherwise ``plaintext``.
        content: The text of the whole file, or of the selected block (see ``range``).
        fingerprint: The group's render fingerprint (what an SE reports it is running).
        range: ``(first, last)`` 1-based inclusive line numbers of the fragment *inside the whole
            file*, or ``None`` when the fragment is the whole file. ``content`` is then the full
            file so the editor can highlight the range in context.
    """

    se_group_id: UUID
    file: str
    language: str
    content: str
    fingerprint: str
    range: tuple[int, int] | None = None


def language_of(path: str) -> str:
    """Return the editor language of a rendered file.

    Example:
        >>> language_of("haproxy/haproxy.cfg"), language_of("certs/web.crtlist")
        ('haproxy', 'plaintext')
    """
    return "haproxy" if path.endswith(".cfg") else "plaintext"


def _sections(config: str) -> dict[str, tuple[int, int]]:
    r"""Map each top-level section header to its 1-based inclusive line range.

    Example:
        >>> _sections("global\n    a\n\nfrontend x\n    b\n")
        {'global': (1, 2), 'frontend x': (4, 5)}
    """
    out: dict[str, tuple[int, int]] = {}
    header: str | None = None
    start = last = 0
    for number, line in enumerate(config.splitlines(), start=1):
        if line and not line[0].isspace() and not line.startswith("#"):
            if header is not None:
                out[header] = (start, last)
            header, start, last = line.strip(), number, number
        elif line.strip() and header is not None:
            last = number
    if header is not None:
        out[header] = (start, last)
    return out


class _Collector:
    def __init__(self, group_id: UUID, result: RenderResult) -> None:
        self.group_id = group_id
        self.result = result
        self.sections = _sections(result.files[CONFIG_PATH])
        self.out: list[Fragment] = []

    def _add(self, file: str, rng: tuple[int, int] | None) -> None:
        self.out.append(
            Fragment(
                self.group_id,
                file,
                language_of(file),
                self.result.files[file],
                self.result.fingerprint,
                rng,
            )
        )

    def section(self, header: str) -> None:
        if header in self.sections:
            self._add(CONFIG_PATH, self.sections[header])

    def file(self, path: str) -> None:
        if path in self.result.files:
            self._add(path, None)

    def lines_containing(self, path: str, needle: str) -> None:
        text = self.result.files.get(path)
        if text is None:
            return
        for number, line in enumerate(text.splitlines(), start=1):
            if needle in line:
                self._add(path, (number, number))


def _service_files(c: _Collector, svc: dm.VirtualService) -> None:
    """Add a service's frontend and the side files it owns."""
    name = esc.ident(svc.name)
    c.section(f"frontend vs_{name}")
    c.file(f"certs/{name}.crtlist")
    c.file(f"haproxy/maps/{name}-host.map")
    c.file(f"haproxy/maps/{name}-wild.map")


def _select(c: _Collector, view: Snapshot, object_type: ObjectType, object_id: UUID) -> None:
    services = [s for s in view.virtual_services if s.enabled and s.protocol is not dm.Protocol.UDP]
    if object_type is ObjectType.SE_GROUP:
        if c.group_id == object_id:
            for path in c.result.files:
                c.file(path)
    elif object_type is ObjectType.VIRTUAL_SERVICE:
        for svc in services:
            if svc.id == object_id:
                _service_files(c, svc)
                if svc.allow_list_id is not None:
                    allow = next(p for p in view.policy_sets if p.id == svc.allow_list_id)
                    c.file(f"haproxy/lists/{esc.ident(allow.name)}.cidr")
    elif object_type is ObjectType.POOL:
        for pool in view.pools:
            if pool.id == object_id:
                c.section(f"backend pool_{esc.ident(pool.name)}")
    elif object_type is ObjectType.HEALTH_MONITOR:
        for pool in sorted(view.pools, key=lambda p: p.name):
            if pool.health_monitor_id == object_id:
                c.section(f"backend pool_{esc.ident(pool.name)}")
    elif object_type is ObjectType.CERTIFICATE:
        cert = next((x for x in view.certificates if x.id == object_id), None)
        if cert is not None:
            for svc in services:
                if object_id in svc.certificate_ids:
                    c.lines_containing(
                        f"certs/{esc.ident(svc.name)}.crtlist", f"/{esc.ident(cert.name)}.pem"
                    )
    elif object_type is ObjectType.POLICY_SET:
        policy = next((p for p in view.policy_sets if p.id == object_id), None)
        if isinstance(policy, dm.AllowListPolicySet):
            c.file(f"haproxy/lists/{esc.ident(policy.name)}.cidr")
        elif policy is not None:
            prefix = f"haproxy/lists/{esc.ident(policy.name)}-"
            for path in sorted(c.result.files):
                if path.startswith(prefix) and path.endswith(".page"):
                    c.file(path)
            for svc in services:
                if object_id in svc.policy_set_ids:
                    c.section(f"frontend vs_{esc.ident(svc.name)}")


def rendered_fragments(
    snapshot: Snapshot,
    object_type: ObjectType,
    object_id: UUID,
    broker_url: str | None = None,
) -> list[Fragment]:
    """Return the rendered fragments one object produced, across every SE group that uses it.

    Groups are visited by name, so the result order is deterministic. A group that cannot be
    rendered is an error for the caller, not an empty answer.

    Args:
        snapshot: Desired state, live or with a change set applied.
        object_type: Kind of object; must be in :data:`RENDERED_TYPES`.
        object_id: The object.
        broker_url: The login broker's public URL, needed by step-up rules (ADR 0195).

    Returns:
        Fragments sorted by SE group name, then in the order the object's text appears. Empty when
        the object renders nothing (for example a disabled or UDP service, or an unused pool).

    Raises:
        ValueError: If ``object_type`` produces no HAProxy text.
        switchyard.render.escape.RenderError: If a value cannot be rendered safely.
        switchyard.domain.errors.DomainValidationError: If the state breaks a domain rule.

    Example:
        >>> from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
        >>> snap = snapshot_from_mapping({
        ...     "tenants": [{"id": "t", "name": "acme"}],
        ...     "networks": [{"id": "n", "tenant_id": "t", "name": "lb", "cidr": "192.0.2.0/24"}],
        ...     "se_groups": [{"id": "g", "tenant_id": "t", "name": "edge", "ha_mode": "vrrp",
        ...                    "networks": [{"network_id": "n", "role": "vip"}]}],
        ...     "pools": [{"id": "p", "tenant_id": "t", "name": "k8s",
        ...                "members": [{"address": "10.0.0.5", "port": 6443}]}],
        ...     "virtual_services": [{"id": "v", "tenant_id": "t", "se_group_id": "g",
        ...                           "name": "api", "protocol": "tcp", "vip": "192.0.2.10",
        ...                           "ports": [{"port": 6443}], "default_pool_id": "p"}],
        ... })
        >>> [f] = rendered_fragments(snap, ObjectType.POOL, UUID(symbolic_uuid("p")))
        >>> f.file, f.range
        ('haproxy/haproxy.cfg', (38, 41))
    """
    if object_type not in RENDERED_TYPES:
        raise ValueError(f"{object_type.value} has no rendered HAProxy text")
    out: list[Fragment] = []
    for group in sorted(snapshot.se_groups, key=lambda g: g.name):
        view = snapshot.for_se_group(group.id)
        collector = _Collector(group.id, render_haproxy(snapshot, group.id, broker_url))
        _select(collector, view, object_type, object_id)
        out += collector.out
    return out

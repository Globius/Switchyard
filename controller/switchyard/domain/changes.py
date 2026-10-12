# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Change operations: how a draft change set turns the current state into a proposed state.

A change set stores operations, not copies of tables (backend design, "Tenancy and edits"). A
preview applies them in memory on top of the latest :class:`~switchyard.domain.snapshot.Snapshot`;
an apply commits the same result. Both call :func:`apply_ops`, so what is previewed is what is
committed.

Semantics:

- ``create``: ``body`` is the whole object; the id comes from the operation, never the body.
- ``update``: ``body`` holds the fields to change (a top-level merge: a list field is replaced as a
  whole). ``expected_version`` must equal the object's version in the base state, otherwise
  :class:`StaleVersionError`.
- ``delete``: removes the object; ``expected_version`` is checked like an update.
- Several operations on one object chain. The version is checked against the base state once, and an
  applied change set raises the version of every object it touched by exactly one.

Example:
    >>> from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
    >>> import uuid
    >>> base = snapshot_from_mapping({"tenants": [{"id": "t", "name": "acme"}]})
    >>> tenant = base.tenants[0].id
    >>> op = ChangeOp(
    ...     object_type=ObjectType.NETWORK,
    ...     object_id=uuid.UUID(int=1),
    ...     action=Action.CREATE,
    ...     body={"name": "lb", "cidr": "192.0.2.0/24"},
    ... )
    >>> draft = apply_ops(base, [op], tenant_id=tenant)
    >>> [(o.status.value, o.name) for o in draft.objects]
    [('created', 'lb')]
"""

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter, ValidationError, model_validator

from switchyard.domain.errors import Problem
from switchyard.domain.models import (
    Certificate,
    Entity,
    HealthMonitor,
    Model,
    Network,
    PolicySet,
    Pool,
    RoutingProfile,
    SeGroup,
    VipPool,
    VirtualService,
)
from switchyard.domain.snapshot import Snapshot, stick_table_entries, validate_snapshot


class ObjectType(enum.StrEnum):
    """Kinds of object a change set can create, update or delete."""

    NETWORK = "network"
    VIP_POOL = "vip_pool"
    SE_GROUP = "se_group"
    VIRTUAL_SERVICE = "virtual_service"
    POOL = "pool"
    HEALTH_MONITOR = "health_monitor"
    CERTIFICATE = "certificate"
    POLICY_SET = "policy_set"
    ROUTING_PROFILE = "routing_profile"


class Action(enum.StrEnum):
    """What an operation does to its object."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


#: Snapshot attribute holding each object type.
COLLECTION: dict[ObjectType, str] = {
    ObjectType.NETWORK: "networks",
    ObjectType.VIP_POOL: "vip_pools",
    ObjectType.SE_GROUP: "se_groups",
    ObjectType.VIRTUAL_SERVICE: "virtual_services",
    ObjectType.POOL: "pools",
    ObjectType.HEALTH_MONITOR: "health_monitors",
    ObjectType.CERTIFICATE: "certificates",
    ObjectType.POLICY_SET: "policy_sets",
    ObjectType.ROUTING_PROFILE: "routing_profiles",
}

_ADAPTERS: dict[ObjectType, TypeAdapter[Any]] = {
    ObjectType.NETWORK: TypeAdapter(Network),
    ObjectType.VIP_POOL: TypeAdapter(VipPool),
    ObjectType.SE_GROUP: TypeAdapter(SeGroup),
    ObjectType.VIRTUAL_SERVICE: TypeAdapter(VirtualService),
    ObjectType.POOL: TypeAdapter(Pool),
    ObjectType.HEALTH_MONITOR: TypeAdapter(HealthMonitor),
    ObjectType.CERTIFICATE: TypeAdapter(Certificate),
    ObjectType.POLICY_SET: TypeAdapter(PolicySet),
    ObjectType.ROUTING_PROFILE: TypeAdapter(RoutingProfile),
}

#: Fields the caller never supplies; the system owns them.
RESERVED_FIELDS = frozenset({"id", "tenant_id", "version"})


class ChangeError(ValueError):
    """Base class of errors raised while applying operations."""


class StaleVersionError(ChangeError):
    """An object changed since the operation's ``expected_version`` was read."""

    def __init__(
        self, object_type: ObjectType, object_id: UUID, expected: int, actual: int
    ) -> None:
        """Record which object is stale."""
        self.object_type, self.object_id = object_type, object_id
        self.expected, self.actual = expected, actual
        super().__init__(
            f"{object_type} {object_id} is at version {actual}, operation expected {expected}"
        )


class UnknownObjectError(ChangeError):
    """An update or delete names an object that does not exist."""

    def __init__(self, object_type: ObjectType, object_id: UUID) -> None:
        """Record which object is missing."""
        self.object_type, self.object_id = object_type, object_id
        super().__init__(f"{object_type} {object_id} does not exist")


class OperationInvalidError(ChangeError):
    """An operation's body does not describe a valid object.

    Attributes:
        problems: Field-level findings; ``path`` is relative to the operation (``body.ports[0]``).
    """

    def __init__(self, index: int, problems: list[Problem]) -> None:
        """Wrap the findings of operation number ``index``."""
        self.index = index
        self.problems = [Problem(f"ops[{index}].{p.path}", p.code, p.message) for p in problems]
        super().__init__("; ".join(f"{p.path}: {p.message} [{p.code}]" for p in self.problems))


class ChangeOp(Model):
    """One operation of a change set.

    Attributes:
        object_type: Kind of object.
        object_id: Id of the object (chosen by the client for ``create``, UUIDv7 recommended).
        action: ``create``, ``update`` or ``delete``.
        body: Object fields (``create``: all of them; ``update``: those to change).
        expected_version: The object's version when the client read it (``update``/``delete``).
    """

    object_type: ObjectType
    object_id: UUID
    action: Action
    body: dict[str, Any] | None = None
    expected_version: int | None = None

    @model_validator(mode="after")
    def _shape(self) -> "ChangeOp":
        if self.action is Action.DELETE:
            if self.body is not None:
                raise ValueError("a delete has no body")
        elif self.body is None:
            raise ValueError(f"a {self.action} needs a body")
        if self.action is Action.CREATE and self.expected_version is not None:
            raise ValueError("a create has no expected_version")
        if self.action is not Action.CREATE and self.expected_version is None:
            raise ValueError(f"a {self.action} needs expected_version")
        if self.body is not None and (reserved := RESERVED_FIELDS & self.body.keys()):
            raise ValueError(f"body must not set {', '.join(sorted(reserved))}")
        return self


class DraftStatus(enum.StrEnum):
    """How an object differs between the base state and a draft."""

    UNCHANGED = "unchanged"
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True)
class DraftObject:
    """One object's place in a draft.

    Attributes:
        object_type: Kind of object.
        object_id: Its id.
        name: Its name (the base name for deleted objects).
        status: ``created``, ``modified`` or ``deleted``.
        changed_fields: Top-level fields that differ (empty for created and deleted objects).
        base_version: Version in the base state (``None`` for created objects).
        new_version: Version after apply (``None`` for deleted objects).
    """

    object_type: ObjectType
    object_id: UUID
    name: str
    status: DraftStatus
    changed_fields: tuple[str, ...] = ()
    base_version: int | None = None
    new_version: int | None = None


@dataclass(frozen=True)
class Draft:
    """The result of applying operations to a base snapshot.

    Attributes:
        snapshot: The proposed state (versions already raised for touched objects).
        objects: Every created, modified and deleted object, sorted by type and name.
        problems: Domain validation findings of ``snapshot`` (empty when it is valid).
    """

    snapshot: Snapshot
    objects: tuple[DraftObject, ...]
    problems: tuple[Problem, ...] = field(default=())

    def status_of(self, object_type: ObjectType, object_id: UUID) -> DraftStatus:
        """Return an object's draft status (``unchanged`` when the draft does not touch it)."""
        for obj in self.objects:
            if (obj.object_type, obj.object_id) == (object_type, object_id):
                return obj.status
        return DraftStatus.UNCHANGED


def _collections(snapshot: Snapshot) -> dict[ObjectType, dict[UUID, Any]]:
    return {t: {o.id: o for o in getattr(snapshot, name)} for t, name in COLLECTION.items()}


def _problems_from(exc: ValidationError) -> list[Problem]:
    out = []
    for err in exc.errors():
        path = ".".join(str(p) for p in err["loc"] if p != "function-after[_shape(), ...]")
        out.append(Problem(path or "body", str(err["type"]), str(err["msg"])))
    return out


def parse_object(object_type: ObjectType, data: dict[str, Any]) -> Entity:
    """Validate ``data`` as an object of ``object_type``.

    Raises:
        pydantic.ValidationError: If the data is not a valid object.
    """
    result: Entity = _ADAPTERS[object_type].validate_python(data)
    return result


def apply_ops(base: Snapshot, ops: Sequence[ChangeOp], *, tenant_id: UUID) -> Draft:
    """Apply ``ops`` to ``base`` in memory and report the proposed state.

    Args:
        base: The latest committed state of the tenant.
        ops: Operations, applied in order.
        tenant_id: Owner of every created object.

    Returns:
        The draft, including domain validation findings (which do not raise).

    Raises:
        StaleVersionError: If an object changed since its ``expected_version``.
        UnknownObjectError: If an update or delete names a missing object.
        OperationInvalidError: If a body is not a valid object or a create reuses an id.
    """
    state = _collections(base)
    base_versions = {(t, i): o.version for t, objs in state.items() for i, o in objs.items()}
    touched: set[tuple[ObjectType, UUID]] = set()
    created: set[tuple[ObjectType, UUID]] = set()
    deleted: dict[tuple[ObjectType, UUID], Any] = {}

    for index, op in enumerate(ops):
        key = (op.object_type, op.object_id)
        objects = state[op.object_type]
        if op.action is Action.CREATE:
            if op.object_id in objects or key in deleted:
                raise OperationInvalidError(
                    index, [Problem("object_id", "duplicate_id", "an object with this id exists")]
                )
            data = {**(op.body or {}), "id": op.object_id, "tenant_id": tenant_id, "version": 1}
            try:
                objects[op.object_id] = parse_object(op.object_type, data)
            except ValidationError as exc:
                raise OperationInvalidError(index, _problems_from(exc)) from exc
            created.add(key)
            continue
        current = objects.get(op.object_id)
        if current is None:
            raise UnknownObjectError(op.object_type, op.object_id)
        if key not in touched and key not in created and op.expected_version != base_versions[key]:
            raise StaleVersionError(
                op.object_type, op.object_id, op.expected_version or 0, base_versions[key]
            )
        touched.add(key)
        if op.action is Action.DELETE:
            del objects[op.object_id]
            if key in created:
                created.discard(key)
                touched.discard(key)
            else:
                deleted[key] = current
            continue
        merged = {**current.model_dump(mode="python"), **(op.body or {})}
        try:
            objects[op.object_id] = parse_object(op.object_type, merged)
        except ValidationError as exc:
            raise OperationInvalidError(index, _problems_from(exc)) from exc

    modified: set[tuple[ObjectType, UUID]] = set()
    originals = _collections(base)
    for key in touched - set(deleted) - created:
        obj = state[key[0]][key[1]]
        original = originals[key[0]][key[1]]
        if list(_changed_fields(original, obj)):
            state[key[0]][key[1]] = obj.model_copy(update={"version": base_versions[key] + 1})
            modified.add(key)
        else:  # touched but unchanged: keep the object exactly as it was, version included
            state[key[0]][key[1]] = original

    after = Snapshot.model_validate(
        {
            "tenants": base.tenants,
            **{COLLECTION[t]: tuple(objs.values()) for t, objs in state.items()},
        }
    )
    summary = _summarise(base, after, created, modified, deleted)
    problems = [*validate_snapshot(after), *quota_problems(base, after)]
    return Draft(snapshot=after, objects=summary, problems=tuple(sorted(problems)))


#: Quota key (besides object type names) that caps the stick-table entries of rate limits.
STICK_TABLE_QUOTA = "stick_table_entries"


def quota_problems(base: Snapshot, after: Snapshot) -> list[Problem]:
    """Report object types that grow past a tenant quota (``quota_exceeded``).

    Quota keys are object type names (``virtual_service``, ``pool``, ...). Only types that gained
    objects are checked, so a tenant already over a lowered quota can still delete and edit.

    Example:
        >>> quota_problems(Snapshot(), Snapshot())
        []
    """
    before, now = _collections(base), _collections(after)
    problems: list[Problem] = []
    for tenant in after.tenants:
        for key, limit in sorted(tenant.quotas.items()):
            if key == STICK_TABLE_QUOTA:
                used = stick_table_entries(after, tenant.id)
                if used > stick_table_entries(base, tenant.id) and used > limit:
                    problems.append(
                        Problem(
                            f"quota.{key}",
                            "quota_exceeded",
                            f"tenant {tenant.name!r} may have {limit} rate-limit table entries; "
                            f"this change would make {used}",
                        )
                    )
                continue
            try:
                object_type = ObjectType(key)
            except ValueError:
                continue
            used = sum(1 for o in now[object_type].values() if o.tenant_id == tenant.id)
            was = sum(1 for o in before[object_type].values() if o.tenant_id == tenant.id)
            if used > was and used > limit:
                problems.append(
                    Problem(
                        f"quota.{key}",
                        "quota_exceeded",
                        f"tenant {tenant.name!r} may have {limit} {key} objects; "
                        f"this change would make {used}",
                    )
                )
    return problems


def _summarise(
    base: Snapshot,
    after: Snapshot,
    created: set[tuple[ObjectType, UUID]],
    modified: set[tuple[ObjectType, UUID]],
    deleted: dict[tuple[ObjectType, UUID], Any],
) -> tuple[DraftObject, ...]:
    before = _collections(base)
    now = _collections(after)
    out: list[DraftObject] = []
    for object_type, object_id in created:
        obj = now[object_type][object_id]
        out.append(
            DraftObject(object_type, object_id, obj.name, DraftStatus.CREATED, new_version=1)
        )
    for object_type, object_id in modified:
        old, new = before[object_type][object_id], now[object_type][object_id]
        changed = tuple(sorted(_changed_fields(old, new)))
        if not changed:
            continue
        out.append(
            DraftObject(
                object_type,
                object_id,
                new.name,
                DraftStatus.MODIFIED,
                changed,
                old.version,
                new.version,
            )
        )
    for (object_type, object_id), old in deleted.items():
        out.append(
            DraftObject(
                object_type, object_id, old.name, DraftStatus.DELETED, base_version=old.version
            )
        )
    return tuple(sorted(out, key=lambda o: (o.object_type, o.name, str(o.object_id))))


def _changed_fields(old: Entity, new: Entity) -> Iterable[str]:
    a, b = old.model_dump(mode="python"), new.model_dump(mode="python")
    return [k for k in a.keys() | b.keys() if k != "version" and a.get(k) != b.get(k)]


def ops_between(current: Snapshot, target: Snapshot) -> list[ChangeOp]:
    """Return the operations that turn ``current`` into ``target`` (used for rollback).

    Objects only in ``target`` are created, objects in both whose fields differ are updated to the
    target's values, objects only in ``current`` are deleted. Tenants are not part of a change set.

    Example:
        >>> from switchyard.domain.loader import snapshot_from_mapping
        >>> empty = snapshot_from_mapping({"tenants": [{"id": "t", "name": "acme"}]})
        >>> ops_between(empty, empty)
        []
    """
    now, want = _collections(current), _collections(target)
    ops: list[ChangeOp] = []
    for object_type in ObjectType:
        for object_id, obj in sorted(want[object_type].items(), key=lambda kv: kv[1].name):
            existing = now[object_type].get(object_id)
            body = {
                k: v for k, v in obj.model_dump(mode="json").items() if k not in RESERVED_FIELDS
            }
            if existing is None:
                ops.append(
                    ChangeOp(
                        object_type=object_type,
                        object_id=object_id,
                        action=Action.CREATE,
                        body=body,
                    )
                )
            elif list(_changed_fields(existing, obj)):
                ops.append(
                    ChangeOp(
                        object_type=object_type,
                        object_id=object_id,
                        action=Action.UPDATE,
                        body=body,
                        expected_version=existing.version,
                    )
                )
        for object_id, existing in sorted(now[object_type].items(), key=lambda kv: kv[1].name):
            if object_id not in want[object_type]:
                ops.append(
                    ChangeOp(
                        object_type=object_type,
                        object_id=object_id,
                        action=Action.DELETE,
                        expected_version=existing.version,
                    )
                )
    return ops

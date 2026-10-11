"""Tests for the locked management tenant rules."""

import uuid
from typing import Any

from switchyard.domain.changes import Action, ChangeOp, ObjectType
from switchyard.domain.loader import symbolic_uuid
from switchyard.domain.management import CONTROLLER_SERVICE, is_locked, protected_problems
from switchyard.domain.models import VirtualService
from switchyard.domain.snapshot import Snapshot
from tests.factories import base_snapshot

VS = uuid.UUID(symbolic_uuid("vs-web"))


def _renamed_snapshot() -> tuple[Snapshot, VirtualService]:
    snap = base_snapshot()
    services = tuple(
        s.model_copy(update={"name": CONTROLLER_SERVICE}) if s.id == VS else s
        for s in snap.virtual_services
    )
    return snap.model_copy(update={"virtual_services": services}), next(
        s for s in services if s.id == VS
    )


def _op(
    action: Action,
    object_type: ObjectType,
    object_id: uuid.UUID,
    body: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> ChangeOp:
    return ChangeOp(
        object_type=object_type,
        object_id=object_id,
        action=action,
        body=body,
        expected_version=expected_version,
    )


def test_only_management_is_locked() -> None:
    assert is_locked("management")
    assert not is_locked("acme")


def test_no_controller_service_means_nothing_protected() -> None:
    snap = base_snapshot()
    ops = [_op(Action.DELETE, ObjectType.VIRTUAL_SERVICE, VS, expected_version=1)]
    assert protected_problems(snap, ops) == []


def test_delete_and_disable_of_controller_vip_are_refused() -> None:
    snap, _ = _renamed_snapshot()
    delete = _op(Action.DELETE, ObjectType.VIRTUAL_SERVICE, VS, expected_version=1)
    disable = _op(
        Action.UPDATE, ObjectType.VIRTUAL_SERVICE, VS, body={"enabled": False}, expected_version=1
    )
    for op in (delete, disable):
        (problem,) = protected_problems(snap, [op])
        assert problem.code == "controller_protected"


def test_deleting_the_serving_se_group_is_refused_but_other_edits_pass() -> None:
    snap, service = _renamed_snapshot()
    drop_group = _op(Action.DELETE, ObjectType.SE_GROUP, service.se_group_id, expected_version=1)
    assert [p.code for p in protected_problems(snap, [drop_group])] == ["controller_protected"]
    rename = _op(
        Action.UPDATE, ObjectType.VIRTUAL_SERVICE, VS, body={"enabled": True}, expected_version=1
    )
    assert protected_problems(snap, [rename]) == []

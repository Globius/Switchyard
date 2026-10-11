"""Tests for change operations and drafts."""

import copy
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain.changes import (
    Action,
    ChangeOp,
    DraftStatus,
    ObjectType,
    OperationInvalidError,
    StaleVersionError,
    UnknownObjectError,
    apply_ops,
    ops_between,
)
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from tests.factories import base_data, base_snapshot

TENANT = uuid.UUID(symbolic_uuid("t-acme"))
POOL = uuid.UUID(symbolic_uuid("pool-web"))
VS = uuid.UUID(symbolic_uuid("vs-web"))


def op(action: Action, object_type: ObjectType, object_id: uuid.UUID, **kw: Any) -> ChangeOp:
    return ChangeOp(object_type=object_type, object_id=object_id, action=action, **kw)


def new_pool(n: int, **body: Any) -> ChangeOp:
    member = {"id": str(uuid.UUID(int=1000 + n)), "address": "10.20.0.77", "port": 80}
    return op(
        Action.CREATE,
        ObjectType.POOL,
        uuid.UUID(int=n),
        body={"name": f"extra{n}", "members": [member], **body},
    )


class TestOperationShape:
    def test_create_needs_body_and_no_version(self) -> None:
        with pytest.raises(ValidationError, match="needs a body"):
            op(Action.CREATE, ObjectType.POOL, POOL)
        with pytest.raises(ValidationError, match="no expected_version"):
            op(Action.CREATE, ObjectType.POOL, POOL, body={}, expected_version=1)

    def test_update_and_delete_need_a_version(self) -> None:
        with pytest.raises(ValidationError, match="needs expected_version"):
            op(Action.UPDATE, ObjectType.POOL, POOL, body={"tls": True})
        with pytest.raises(ValidationError, match="needs expected_version"):
            op(Action.DELETE, ObjectType.POOL, POOL)

    def test_delete_has_no_body(self) -> None:
        with pytest.raises(ValidationError, match="no body"):
            op(Action.DELETE, ObjectType.POOL, POOL, body={}, expected_version=1)

    @pytest.mark.parametrize("reserved", ["id", "tenant_id", "version"])
    def test_system_fields_cannot_be_set(self, reserved: str) -> None:
        with pytest.raises(ValidationError, match=reserved):
            op(Action.CREATE, ObjectType.POOL, POOL, body={reserved: 1})


class TestApply:
    def test_create_adds_a_valid_object_owned_by_the_tenant(self) -> None:
        draft = apply_ops(base_snapshot(), [new_pool(5)], tenant_id=TENANT)
        created = next(p for p in draft.snapshot.pools if p.name == "extra5")
        assert (created.tenant_id, created.version) == (TENANT, 1)
        assert [(o.status, o.name) for o in draft.objects] == [(DraftStatus.CREATED, "extra5")]
        assert draft.problems == ()

    def test_invalid_body_reports_field_paths(self) -> None:
        bad = op(
            Action.CREATE,
            ObjectType.POOL,
            uuid.UUID(int=9),
            body={"name": "Bad Name", "algorithm": "nope"},
        )
        with pytest.raises(OperationInvalidError) as exc:
            apply_ops(base_snapshot(), [bad], tenant_id=TENANT)
        paths = {p.path for p in exc.value.problems}
        assert {"ops[0].name", "ops[0].algorithm"} <= paths

    def test_unknown_fields_are_rejected(self) -> None:
        bad = op(
            Action.CREATE, ObjectType.POOL, uuid.UUID(int=9), body={"name": "p", "surprise": 1}
        )
        with pytest.raises(OperationInvalidError, match="surprise"):
            apply_ops(base_snapshot(), [bad], tenant_id=TENANT)

    def test_duplicate_id_on_create(self) -> None:
        with pytest.raises(OperationInvalidError, match="duplicate_id"):
            apply_ops(base_snapshot(), [new_pool(5), new_pool(5)], tenant_id=TENANT)
        again = op(Action.CREATE, ObjectType.POOL, POOL, body={"name": "other"})
        with pytest.raises(OperationInvalidError):
            apply_ops(base_snapshot(), [again], tenant_id=TENANT)

    def test_update_merges_top_level_fields_and_bumps_version_once(self) -> None:
        ops = [
            op(Action.UPDATE, ObjectType.POOL, POOL, body={"tls": True}, expected_version=1),
            op(
                Action.UPDATE,
                ObjectType.POOL,
                POOL,
                body={"algorithm": "leastconn"},
                expected_version=1,
            ),
        ]
        draft = apply_ops(base_snapshot(), ops, tenant_id=TENANT)
        pool = draft.snapshot.pools[0]
        assert (pool.tls, pool.algorithm.value, pool.version) == (True, "leastconn", 2)
        assert pool.members == base_snapshot().pools[0].members
        (obj,) = draft.objects
        assert (obj.status, obj.changed_fields, obj.base_version, obj.new_version) == (
            DraftStatus.MODIFIED,
            ("algorithm", "tls"),
            1,
            2,
        )

    def test_list_fields_are_replaced_not_merged(self) -> None:
        member = {"id": str(uuid.UUID(int=77)), "address": "10.20.0.99", "port": 9000}
        ops = [
            op(Action.UPDATE, ObjectType.POOL, POOL, body={"members": [member]}, expected_version=1)
        ]
        pool = apply_ops(base_snapshot(), ops, tenant_id=TENANT).snapshot.pools[0]
        assert [str(m.address) for m in pool.members] == ["10.20.0.99"]

    def test_stale_expected_version(self) -> None:
        data = base_data()
        data["pools"][0]["version"] = 4
        stale = op(Action.UPDATE, ObjectType.POOL, POOL, body={"tls": True}, expected_version=3)
        with pytest.raises(StaleVersionError) as exc:
            apply_ops(snapshot_from_mapping(data), [stale], tenant_id=TENANT)
        assert (exc.value.expected, exc.value.actual) == (3, 4)

    def test_unknown_object(self) -> None:
        missing = op(Action.DELETE, ObjectType.POOL, uuid.UUID(int=404), expected_version=1)
        with pytest.raises(UnknownObjectError):
            apply_ops(base_snapshot(), [missing], tenant_id=TENANT)

    def test_update_that_produces_an_invalid_object(self) -> None:
        bad = op(
            Action.UPDATE, ObjectType.POOL, POOL, body={"tls_verify": True}, expected_version=1
        )
        with pytest.raises(OperationInvalidError, match="tls_verify"):
            apply_ops(base_snapshot(), [bad], tenant_id=TENANT)

    def test_delete_of_a_referenced_object_is_reported_as_a_problem(self) -> None:
        draft = apply_ops(
            base_snapshot(),
            [op(Action.DELETE, ObjectType.POOL, POOL, expected_version=1)],
            tenant_id=TENANT,
        )
        assert [p.code for p in draft.problems] == ["unknown_reference"]
        assert [(o.status, o.name) for o in draft.objects] == [(DraftStatus.DELETED, "web")]

    def test_deleting_a_referencing_object_first_then_the_pool_is_clean(self) -> None:
        ops = [
            op(Action.DELETE, ObjectType.VIRTUAL_SERVICE, VS, expected_version=1),
            op(Action.DELETE, ObjectType.POOL, POOL, expected_version=1),
        ]
        draft = apply_ops(base_snapshot(), ops, tenant_id=TENANT)
        assert draft.problems == ()
        assert draft.snapshot.pools == ()

    def test_create_then_delete_leaves_no_trace(self) -> None:
        ops = [
            new_pool(5),
            op(Action.DELETE, ObjectType.POOL, uuid.UUID(int=5), expected_version=1),
        ]
        draft = apply_ops(base_snapshot(), ops, tenant_id=TENANT)
        assert draft.objects == ()
        assert draft.snapshot == base_snapshot()

    def test_create_then_update_chains(self) -> None:
        ops = [
            new_pool(5),
            op(
                Action.UPDATE,
                ObjectType.POOL,
                uuid.UUID(int=5),
                body={"tls": True},
                expected_version=1,
            ),
        ]
        draft = apply_ops(base_snapshot(), ops, tenant_id=TENANT)
        created = next(p for p in draft.snapshot.pools if p.name == "extra5")
        assert (created.tls, created.version) == (True, 1)

    def test_noop_update_is_not_reported(self) -> None:
        ops = [op(Action.UPDATE, ObjectType.POOL, POOL, body={"tls": False}, expected_version=1)]
        draft = apply_ops(base_snapshot(), ops, tenant_id=TENANT)
        assert draft.objects == ()

    def test_base_snapshot_is_never_mutated(self) -> None:
        base = base_snapshot()
        before = copy.deepcopy(base.model_dump())
        apply_ops(base, [new_pool(5)], tenant_id=TENANT)
        assert base.model_dump() == before

    def test_policy_sets_are_parsed_by_kind(self) -> None:
        create = op(
            Action.CREATE,
            ObjectType.POLICY_SET,
            uuid.UUID(int=6),
            body={"name": "cf", "kind": "allow_list", "cidrs": ["198.51.100.0/24"]},
        )
        draft = apply_ops(base_snapshot(), [create], tenant_id=TENANT)
        assert draft.snapshot.policy_sets[0].kind == "allow_list"

    def test_status_lookup(self) -> None:
        draft = apply_ops(base_snapshot(), [new_pool(5)], tenant_id=TENANT)
        assert draft.status_of(ObjectType.POOL, uuid.UUID(int=5)) is DraftStatus.CREATED
        assert draft.status_of(ObjectType.POOL, POOL) is DraftStatus.UNCHANGED


class TestOpsBetween:
    def test_identical_snapshots_need_no_operations(self) -> None:
        assert ops_between(base_snapshot(), base_snapshot()) == []

    def test_applying_the_operations_reaches_the_target(self) -> None:
        current = base_snapshot()
        target_data = base_data()
        target_data["pools"][0]["tls"] = True
        target_data["pools"].append({"id": "pool-new", "tenant_id": "t-acme", "name": "added"})
        del target_data["virtual_services"][0]["routes"][0]
        target_data["virtual_services"][0]["default_pool_id"] = "pool-web"
        target = snapshot_from_mapping(target_data)
        ops = ops_between(current, target)
        assert {o.action for o in ops} == {Action.CREATE, Action.UPDATE}
        reached = apply_ops(current, ops, tenant_id=TENANT).snapshot

        def strip(s: Any) -> Any:
            d = s.model_dump(mode="json")
            for coll in d.values():
                for obj in coll:
                    obj.pop("version", None)
            return d

        assert strip(reached) == strip(target)

    def test_removed_objects_become_deletes(self) -> None:
        target_data = base_data()
        target_data["virtual_services"] = []
        ops = ops_between(base_snapshot(), snapshot_from_mapping(target_data))
        assert [(o.action, o.object_type) for o in ops] == [
            (Action.DELETE, ObjectType.VIRTUAL_SERVICE)
        ]


class TestQuotas:
    def _limited(self, limit: int) -> Any:
        base = base_snapshot()
        tenants = tuple(t.model_copy(update={"quotas": {"pool": limit}}) for t in base.tenants)
        return base.model_copy(update={"tenants": tenants})

    def test_growth_past_the_limit_is_a_problem(self) -> None:
        base = self._limited(len(base_snapshot().pools))
        draft = apply_ops(base, [new_pool(1)], tenant_id=TENANT)
        assert [p.code for p in draft.problems] == ["quota_exceeded"]
        assert draft.problems[0].path == "quota.pool"

    def test_growth_within_the_limit_is_fine(self) -> None:
        base = self._limited(len(base_snapshot().pools) + 1)
        assert not apply_ops(base, [new_pool(1)], tenant_id=TENANT).problems

    def test_a_tenant_over_a_lowered_quota_can_still_edit(self) -> None:
        base = self._limited(0)
        edit = op(Action.UPDATE, ObjectType.POOL, POOL, body={"tls": True}, expected_version=1)
        assert not apply_ops(base, [edit], tenant_id=TENANT).problems

    def test_unknown_quota_keys_are_ignored_by_the_check(self) -> None:
        base = base_snapshot()
        tenants = tuple(t.model_copy(update={"quotas": {"max_vs": 0}}) for t in base.tenants)
        base = base.model_copy(update={"tenants": tenants})
        assert not apply_ops(base, [new_pool(1)], tenant_id=TENANT).problems

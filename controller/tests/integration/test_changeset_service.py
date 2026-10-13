# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Change-set lifecycle, roles and approval, against a real database."""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from switchyard.domain.changes import (
    Action,
    ChangeOp,
    DraftStatus,
    ObjectType,
    OperationInvalidError,
)
from switchyard.domain.workflow import ChangeSetState, Role
from switchyard.services.changesets import ValidationFailedError
from switchyard.services.errors import ConflictError, NotFoundError, PermissionDeniedError
from tests.pipeline_env import (
    POOL,
    TENANT,
    VS,
    World,
    add_host_route,
    principal,
    update_pool,
    world,
    world_data,
)

pytestmark = pytest.mark.usefixtures("owner_engine")


async def scalar(w: World, sql: str, **params: object) -> Any:
    from switchyard.db.tenancy import admin_session

    async with admin_session(w.db.admin) as s:
        return (await s.execute(text(sql), params)).scalar_one()


def approval_data() -> dict[str, Any]:
    data = world_data()
    data["tenants"][0]["require_approval"] = True
    return data


class TestRoles:
    async def test_viewers_cannot_open_change_sets(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            with pytest.raises(PermissionDeniedError):
                await w.service.open(principal(Role.VIEWER), TENANT)

    async def test_operators_change_applications_but_not_infrastructure(self, pg_url: str) -> None:
        network = ChangeOp(
            object_type=ObjectType.NETWORK,
            object_id=uuid.UUID(int=5),
            action=Action.CREATE,
            body={"name": "extra", "cidr": "198.51.100.0/24"},
        )
        async with world(pg_url) as w:
            operator = principal(Role.OPERATOR, "olga")
            detail = await w.service.open(operator, TENANT)
            await w.service.add_ops(operator, TENANT, detail.record.id, [update_pool(tls=True)])
            with pytest.raises(PermissionDeniedError):
                await w.service.add_ops(operator, TENANT, detail.record.id, [network])
            admin = principal(Role.TENANT_ADMIN)
            await w.service.add_ops(admin, TENANT, detail.record.id, [network])

    async def test_other_tenants_are_out_of_reach(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            outsider = principal(tenant=uuid.UUID(int=999))
            with pytest.raises(PermissionDeniedError):
                await w.service.open(outsider, TENANT)
            detail = await w.service.open(principal(), TENANT)
            with pytest.raises(PermissionDeniedError):
                await w.service.get(outsider, TENANT, detail.record.id)

    async def test_a_system_administrator_may_act_in_any_tenant(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            detail = await w.service.open(principal(Role.SYSTEM_ADMIN, "root"), TENANT)
            assert detail.record.author == "user:root"


class TestEditing:
    async def test_invalid_operations_are_refused_and_nothing_is_stored(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            detail = await w.service.open(principal(), TENANT)
            bad = update_pool(algorithm="nope")
            with pytest.raises(OperationInvalidError):
                await w.service.add_ops(principal(), TENANT, detail.record.id, [bad])
            assert (await w.service.get(principal(), TENANT, detail.record.id)).ops == ()

    async def test_member_ids_are_fixed_when_the_operation_is_stored(self, pg_url: str) -> None:
        create = ChangeOp(
            object_type=ObjectType.POOL,
            object_id=uuid.UUID(int=6),
            action=Action.CREATE,
            body={"name": "extra", "members": [{"address": "10.20.0.88", "port": 80}]},
        )
        async with world(pg_url) as w:
            detail = await w.service.open(principal(), TENANT)
            stored = await w.service.add_ops(principal(), TENANT, detail.record.id, [create])
            assert stored.ops[0].body is not None
            member_id = stored.ops[0].body["members"][0]["id"]
            preview = await w.service.preview(principal(), TENANT, detail.record.id)
            again = await w.service.preview(principal(), TENANT, detail.record.id)
            ids = [
                str(m.id)
                for p in preview.draft.snapshot.pools
                for m in p.members
                if p.name == "extra"
            ]
            ids_again = [
                str(m.id)
                for p in again.draft.snapshot.pools
                for m in p.members
                if p.name == "extra"
            ]
            assert ids == ids_again == [member_id]

    async def test_remove_operation(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            detail = await w.service.open(principal(), TENANT)
            await w.service.add_ops(
                principal(),
                TENANT,
                detail.record.id,
                [update_pool(tls=True), add_host_route("x.example.test")],
            )
            after = await w.service.remove_op(principal(), TENANT, detail.record.id, 0)
            assert [o.object_type for o in after.ops] == [ObjectType.VIRTUAL_SERVICE]
            with pytest.raises(NotFoundError):
                await w.service.remove_op(principal(), TENANT, detail.record.id, 5)

    async def test_discard_and_terminal_states(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            detail = await w.service.open(principal(), TENANT)
            record = await w.service.discard(principal(), TENANT, detail.record.id)
            assert record.state is ChangeSetState.DISCARDED
            with pytest.raises(ConflictError):
                await w.service.add_ops(
                    principal(), TENANT, detail.record.id, [update_pool(tls=True)]
                )
            with pytest.raises(ConflictError):
                await w.service.discard(principal(), TENANT, detail.record.id)

    async def test_list_filters_by_state(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            kept = await w.service.open(principal(), TENANT, "kept")
            dropped = await w.service.open(principal(), TENANT, "dropped")
            await w.service.discard(principal(), TENANT, dropped.record.id)
            drafts = await w.service.list_change_sets(
                principal(), TENANT, states={ChangeSetState.DRAFT}
            )
            assert [c.id for c in drafts] == [kept.record.id]


class TestPreview:
    async def test_diff_shows_the_rendered_configuration_change_per_group(
        self, pg_url: str
    ) -> None:
        async with world(pg_url) as w:
            detail = await w.service.open(principal(), TENANT)
            await w.service.add_ops(
                principal(), TENANT, detail.record.id, [add_host_route("new.example.test")]
            )
            preview = await w.service.preview(principal(), TENANT, detail.record.id)
            (group,) = preview.groups
            changed = {f.path: f for f in group.files}
            assert set(changed) == {"haproxy/haproxy.cfg", "haproxy/maps/web-host.map"}
            assert changed["haproxy/maps/web-host.map"].status == "modified"
            assert "+new.example.test pool_web" in changed["haproxy/maps/web-host.map"].diff
            assert [(o.status, o.name) for o in preview.draft.objects] == [
                (DraftStatus.MODIFIED, "web")
            ]

    async def test_preview_does_not_touch_the_database_or_engines(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            before = await w.snapshot()
            detail = await w.service.open(principal(), TENANT)
            await w.service.add_ops(
                principal(), TENANT, detail.record.id, [add_host_route("new.example.test")]
            )
            await w.service.preview(principal(), TENANT, detail.record.id)
            assert await w.snapshot() == before
            assert all(e.pushes == [] for e in w.engines.values())

    async def test_draft_view_marks_objects(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            detail = await w.service.open(principal(), TENANT)
            await w.service.add_ops(principal(), TENANT, detail.record.id, [update_pool(tls=True)])
            _, draft = await w.service.draft_view(principal(), TENANT, detail.record.id)
            assert draft.status_of(ObjectType.POOL, POOL) is DraftStatus.MODIFIED
            assert draft.status_of(ObjectType.VIRTUAL_SERVICE, VS) is DraftStatus.UNCHANGED


class TestApplyPreflight:
    async def test_apply_refuses_a_state_that_breaks_domain_rules_and_queues_nothing(
        self, pg_url: str
    ) -> None:
        delete_pool = ChangeOp(
            object_type=ObjectType.POOL, object_id=POOL, action=Action.DELETE, expected_version=1
        )
        async with world(pg_url) as w:
            detail = await w.service.open(principal(), TENANT)
            await w.service.add_ops(principal(), TENANT, detail.record.id, [delete_pool])
            with pytest.raises(ValidationFailedError) as exc:
                await w.service.apply(principal(), TENANT, detail.record.id)
            assert [p.code for p in exc.value.problems] == ["unknown_reference"]
            assert await scalar(w, "SELECT count(*) FROM job") == 0
            record = (await w.service.get(principal(), TENANT, detail.record.id)).record
            assert record.state is ChangeSetState.DRAFT

    async def test_apply_queues_one_job_and_marks_the_change_set_applying(
        self, pg_url: str
    ) -> None:
        async with world(pg_url) as w:
            detail = await w.service.open(principal(), TENANT)
            await w.service.add_ops(principal(), TENANT, detail.record.id, [update_pool(tls=True)])
            job_id = await w.service.apply(
                principal(), TENANT, detail.record.id, request_id="req-1"
            )
            assert (
                await scalar(w, "SELECT kind FROM job WHERE id = :i", i=job_id)
                == "apply_change_set"
            )
            assert await scalar(w, "SELECT request_id FROM job WHERE id = :i", i=job_id) == "req-1"
            record = (await w.service.get(principal(), TENANT, detail.record.id)).record
            assert record.state is ChangeSetState.APPLYING
            with pytest.raises(ConflictError):
                await w.service.apply(principal(), TENANT, detail.record.id)


class TestApproval:
    async def test_full_four_eyes_flow(self, pg_url: str) -> None:
        async with world(pg_url, approval_data()) as w:
            author = principal(Role.OPERATOR, "olga")
            detail = await w.service.open(author, TENANT)
            await w.service.add_ops(author, TENANT, detail.record.id, [update_pool(tls=True)])
            with pytest.raises(PermissionDeniedError, match="requires approval"):
                await w.service.apply(author, TENANT, detail.record.id)

            pending = await w.service.request_approval(author, TENANT, detail.record.id)
            assert pending.state is ChangeSetState.PENDING_APPROVAL

            with pytest.raises(PermissionDeniedError):  # operators cannot approve
                await w.service.approve(author, TENANT, detail.record.id)
            admin_author = principal(Role.TENANT_ADMIN, "olga")
            with pytest.raises(PermissionDeniedError, match="wrote it"):
                await w.service.approve(admin_author, TENANT, detail.record.id)

            reviewer = principal(Role.TENANT_ADMIN, "bob")
            job_id = await w.service.approve(reviewer, TENANT, detail.record.id)
            record = (await w.service.get(reviewer, TENANT, detail.record.id)).record
            assert (record.state, record.approver) == (ChangeSetState.APPLYING, "user:bob")
            assert (
                await scalar(w, "SELECT kind FROM job WHERE id = :i", i=job_id)
                == "apply_change_set"
            )
            report = await w.pipeline.run(TENANT, detail.record.id)
            assert report.ok, report.error

    async def test_editing_voids_a_pending_request(self, pg_url: str) -> None:
        async with world(pg_url, approval_data()) as w:
            author = principal(Role.OPERATOR, "olga")
            detail = await w.service.open(author, TENANT)
            await w.service.add_ops(author, TENANT, detail.record.id, [update_pool(tls=True)])
            await w.service.request_approval(author, TENANT, detail.record.id)
            await w.service.add_ops(
                author, TENANT, detail.record.id, [add_host_route("x.example.test")]
            )
            record = (await w.service.get(author, TENANT, detail.record.id)).record
            assert record.state is ChangeSetState.DRAFT
            assert (
                await scalar(
                    w,
                    "SELECT count(*) FROM audit_event WHERE action = 'change_set.approval_voided'",
                )
                == 1
            )

    async def test_rejection_returns_the_draft_with_a_comment(self, pg_url: str) -> None:
        async with world(pg_url, approval_data()) as w:
            author = principal(Role.OPERATOR, "olga")
            detail = await w.service.open(author, TENANT)
            await w.service.add_ops(author, TENANT, detail.record.id, [update_pool(tls=True)])
            await w.service.request_approval(author, TENANT, detail.record.id)
            rejected = await w.service.reject(
                principal(Role.TENANT_ADMIN, "bob"), TENANT, detail.record.id, "not now"
            )
            assert (rejected.state, rejected.reject_comment) == (ChangeSetState.DRAFT, "not now")

    async def test_break_glass_needs_a_system_administrator_and_a_reason(self, pg_url: str) -> None:
        async with world(pg_url, approval_data()) as w:
            detail = await w.service.open(principal(Role.TENANT_ADMIN), TENANT)
            await w.service.add_ops(principal(), TENANT, detail.record.id, [update_pool(tls=True)])
            root = principal(Role.SYSTEM_ADMIN, "root")
            with pytest.raises(PermissionDeniedError):
                await w.service.apply(
                    principal(Role.TENANT_ADMIN),
                    TENANT,
                    detail.record.id,
                    break_glass_reason="outage",
                )
            with pytest.raises(PermissionDeniedError):
                await w.service.apply(root, TENANT, detail.record.id)
            await w.service.apply(root, TENANT, detail.record.id, break_glass_reason="outage")
            assert await scalar(w, "SELECT break_glass_reason FROM change_set") == "outage"
            assert (
                await scalar(
                    w,
                    "SELECT count(*) FROM audit_event WHERE action = 'change_set.break_glass_apply'",
                )
                == 1
            )

    async def test_management_tenant_is_locked_and_protects_the_controller_vip(
        self, pg_url: str
    ) -> None:
        data = world_data()
        data["tenants"][0]["name"] = "management"
        data["virtual_services"][0]["name"] = "controller"
        for cert in data.get("certificates", []):
            cert["secret_ref"] = cert["secret_ref"].replace("tenants/acme/", "tenants/management/")
        async with world(pg_url, data) as w:
            delete = ChangeOp(
                object_type=ObjectType.VIRTUAL_SERVICE,
                object_id=VS,
                action=Action.DELETE,
                expected_version=1,
            )
            detail = await w.service.open(principal(Role.TENANT_ADMIN), TENANT)
            with pytest.raises(ValidationFailedError) as refused:
                await w.service.add_ops(
                    principal(Role.TENANT_ADMIN), TENANT, detail.record.id, [delete]
                )
            assert refused.value.problems[0].code == "controller_protected"
            await w.service.add_ops(principal(), TENANT, detail.record.id, [update_pool(tls=True)])
            with pytest.raises(PermissionDeniedError):  # locked: tenant admins cannot apply
                await w.service.apply(principal(Role.TENANT_ADMIN), TENANT, detail.record.id)
            root = principal(Role.SYSTEM_ADMIN, "root")
            with pytest.raises(PermissionDeniedError):  # a reason is required
                await w.service.apply(root, TENANT, detail.record.id)
            await w.service.apply(root, TENANT, detail.record.id, break_glass_reason="rotate pool")

    async def test_only_drafts_can_be_submitted(self, pg_url: str) -> None:
        async with world(pg_url, approval_data()) as w:
            detail = await w.service.open(principal(Role.OPERATOR, "olga"), TENANT)
            await w.service.discard(principal(Role.OPERATOR, "olga"), TENANT, detail.record.id)
            with pytest.raises(ConflictError):
                await w.service.request_approval(
                    principal(Role.OPERATOR, "olga"), TENANT, detail.record.id
                )


class TestAuditTrail:
    async def test_every_step_is_recorded_with_the_actor(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            report = await w.run_change([add_host_route("new.example.test")])
            assert report.ok
            from switchyard.db.tenancy import admin_session

            async with admin_session(w.db.admin) as s:
                rows = (
                    await s.execute(text("SELECT action, actor FROM audit_event ORDER BY at, id"))
                ).all()
            assert [r[0] for r in rows] == [
                "change_set.open",
                "change_set.add_ops",
                "change_set.apply",
                "change_set.applied",
            ]
            assert {r[1] for r in rows} == {"user:alice"}

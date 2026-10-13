# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The apply pipeline against a real database and fake engines."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.adapters.fake_haproxy import FakeHaproxy
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.changes import Action, ChangeOp, ObjectType, StaleVersionError
from switchyard.domain.workflow import ChangeSetState, DeploymentState, EngineState
from switchyard.pipeline.apply import PipelineStateError
from switchyard.render.haproxy import render_haproxy
from switchyard.services.errors import ConflictError, NotFoundError
from tests.pipeline_env import (
    GROUP,
    TENANT,
    World,
    add_host_route,
    principal,
    update_pool,
    world,
    world_data,
)

pytestmark = pytest.mark.usefixtures("owner_engine")


async def scalar(w: World, sql: str, **params: object) -> object:
    async with admin_session(w.db.admin) as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def test_successful_apply_updates_engines_database_and_history(pg_url: str) -> None:
    async with world(pg_url) as w:
        before = await w.snapshot()
        report = await w.run_change([add_host_route("new.example.test")])
        assert report.ok, report.error
        assert report.revision_number == 1

        after = await w.snapshot()
        expected = render_haproxy(after, GROUP)
        assert {n: e.files["haproxy/haproxy.cfg"] for n, e in w.engines.items()} == {
            "se-a": expected.files["haproxy/haproxy.cfg"],
            "se-b": expected.files["haproxy/haproxy.cfg"],
        }
        assert "new.example.test" in w.engines["se-a"].files["haproxy/maps/web-host.map"]
        assert render_haproxy(before, GROUP).fingerprint != expected.fingerprint

        web = next(v for v in after.virtual_services if v.name == "web")
        assert (web.version, len(web.routes)) == (2, 2)
        assert await scalar(w, "SELECT state FROM change_set") == "applied"
        assert await scalar(w, "SELECT number FROM revision") == 1
        assert await scalar(w, "SELECT state FROM deployment") == "succeeded"
        assert await scalar(w, "SELECT rendered_hash FROM deployment") == expected.content_hash
        assert (
            await scalar(w, "SELECT count(*) FROM audit_event WHERE action = 'change_set.applied'")
            == 1
        )
        assert await scalar(w, "SELECT count(*) FROM job WHERE kind = 'git_mirror'") == 1


async def test_engines_are_updated_one_at_a_time_canary_first(pg_url: str) -> None:
    async with world(pg_url) as w:
        order: list[str] = []

        for name, engine in w.engines.items():

            async def record(e: FakeHaproxy, name: str = name) -> None:
                order.append(name)

            engine.on_push = record
        assert (await w.run_change([add_host_route("new.example.test")])).ok
        assert order == ["se-a", "se-b"]


async def test_canary_regression_reverts_the_canary_and_never_touches_the_rest(
    pg_url: str,
) -> None:
    async with world(pg_url) as w:
        canary, other = w.engines["se-a"], w.engines["se-b"]
        original = canary.files["haproxy/haproxy.cfg"]

        async def degrade(engine: FakeHaproxy) -> None:
            if engine.files["haproxy/haproxy.cfg"] != original:  # only the new config is bad
                engine.set_status("pool_web", "10-20-0-11-8080", "DOWN")

        canary.on_push = degrade
        report = await w.run_change([add_host_route("new.example.test")])

        assert not report.ok
        assert "went from up to down" in (report.error or "")
        assert canary.files["haproxy/haproxy.cfg"] == original, "canary was reverted"
        assert other.pushes == [], "the other engine never saw the change"
        assert len(canary.pushes) == 2, "push, then revert"
        assert await scalar(w, "SELECT state FROM change_set") == "failed"
        assert await scalar(w, "SELECT count(*) FROM revision") == 0
        web = next(v for v in (await w.snapshot()).virtual_services if v.name == "web")
        assert (web.version, len(web.routes)) == (1, 1), "desired state is unchanged"
        assert await scalar(w, "SELECT state FROM deployment") == "failed"


async def test_failure_on_a_later_engine_reverts_every_engine(pg_url: str) -> None:
    async with world(pg_url) as w:
        original = w.engines["se-a"].files["haproxy/haproxy.cfg"]
        w.engines["se-b"].fail_next("reload")
        report = await w.run_change([add_host_route("new.example.test")])
        assert not report.ok
        assert w.engines["se-a"].files["haproxy/haproxy.cfg"] == original
        assert w.engines["se-b"].files["haproxy/haproxy.cfg"] == original
        group = report.groups[0]
        assert group.state is DeploymentState.FAILED
        assert any("reverted" in line for line in group.details["reverted"])


async def test_rejected_by_the_canary_engines_own_haproxy(pg_url: str) -> None:
    async with world(pg_url) as w:
        w.engines["se-a"].fail_next("rejected")
        report = await w.run_change([add_host_route("new.example.test")])
        assert not report.ok
        assert "rejected" in (report.error or "")
        assert w.engines["se-a"].pushes == []
        assert w.engines["se-b"].pushes == []


async def test_group_without_enabled_engines_fails_before_touching_anything(pg_url: str) -> None:
    async with world(pg_url) as w:
        w.use_engines("se-a", states={"se-a": EngineState.DISABLED})
        await w.prime()
        report = await w.run_change([add_host_route("new.example.test")])
        assert not report.ok
        assert "no enabled engines" in (report.error or "")
        assert w.engines["se-a"].pushes == []
        assert await scalar(w, "SELECT state FROM change_set") == "failed"


async def test_a_change_that_does_not_alter_any_render_pushes_nothing(pg_url: str) -> None:
    data = world_data()
    data["pools"].append({"id": "pool-spare", "tenant_id": "t-acme", "name": "spare"})
    async with world(pg_url, data) as w:
        spare = next(p.id for p in (await w.snapshot()).pools if p.name == "spare")
        op = ChangeOp(
            object_type=ObjectType.POOL,
            object_id=spare,
            action=Action.UPDATE,
            body={"algorithm": "leastconn"},
            expected_version=1,
        )
        report = await w.run_change([op])
        assert report.ok
        assert report.groups == ()
        assert all(e.pushes == [] for e in w.engines.values())
        assert report.revision_number == 1, "the desired state still changed"


async def test_stale_operation_is_refused_before_anything_is_queued(pg_url: str) -> None:
    async with world(pg_url) as w:
        detail = await w.service.open(principal(), TENANT, "stale")
        await w.service.add_ops(principal(), TENANT, detail.record.id, [update_pool(tls=True)])
        # someone else changes the pool first
        assert (await w.run_change([update_pool(algorithm="leastconn")])).ok
        with pytest.raises(StaleVersionError):
            await w.service.apply(principal(), TENANT, detail.record.id)
        assert await scalar(w, "SELECT count(*) FROM job WHERE kind = 'apply_change_set'") == 1


async def test_database_change_during_rollout_reverts_engines(pg_url: str) -> None:
    async with world(pg_url) as w:
        original = w.engines["se-b"].files["haproxy/haproxy.cfg"]

        async def interfere(engine: FakeHaproxy) -> None:
            async with admin_session(w.db.admin) as s:
                await s.execute(text("UPDATE virtual_service SET version = 9"))

        w.engines["se-b"].on_push = interfere
        report = await w.run_change([add_host_route("new.example.test")])
        assert not report.ok
        assert "database changed during the rollout" in (report.error or "")
        assert w.engines["se-a"].files["haproxy/haproxy.cfg"] == original
        assert w.engines["se-b"].files["haproxy/haproxy.cfg"] == original
        assert await scalar(w, "SELECT count(*) FROM revision") == 0


async def test_running_a_change_set_that_is_not_applying_is_refused(pg_url: str) -> None:
    async with world(pg_url) as w:
        detail = await w.service.open(principal(), TENANT, "never applied")
        with pytest.raises(PipelineStateError):
            await w.pipeline.run(TENANT, detail.record.id)


async def test_the_apply_message_lands_on_the_revision(pg_url: str) -> None:
    async with world(pg_url) as w:
        assert (
            await w.run_change([add_host_route("one.example.test")], message="  OPS-1 why  ")
        ).ok
        assert (await w.run_change([add_host_route("two.example.test", expected=2)])).ok
        revisions = {r.number: r.message for r in await w.service.revisions(principal(), TENANT)}
        assert revisions == {1: "OPS-1 why", 2: ""}


async def test_rollback_restores_an_earlier_revision_through_the_same_pipeline(pg_url: str) -> None:
    async with world(pg_url) as w:
        assert (await w.run_change([add_host_route("one.example.test")])).ok
        at_revision_1 = w.engines["se-a"].files["haproxy/haproxy.cfg"]
        assert (await w.run_change([add_host_route("two.example.test", expected=2)])).ok
        assert w.engines["se-a"].files["haproxy/haproxy.cfg"] != at_revision_1

        detail = await w.service.rollback(principal(), TENANT, 1)
        assert detail.record.summary == "Roll back to revision 1"
        assert detail.record.state is ChangeSetState.DRAFT
        await w.service.apply(principal(), TENANT, detail.record.id)
        report = await w.pipeline.run(TENANT, detail.record.id)

        assert report.ok, report.error
        assert report.revision_number == 3
        assert w.engines["se-a"].files["haproxy/haproxy.cfg"] == at_revision_1
        assert w.engines["se-b"].files["haproxy/haproxy.cfg"] == at_revision_1
        web = next(v for v in (await w.snapshot()).virtual_services if v.name == "web")
        assert (len(web.routes), web.version) == (2, 4), "version keeps counting up"


async def test_rollback_to_the_current_state_is_refused(pg_url: str) -> None:
    async with world(pg_url) as w:
        assert (await w.run_change([add_host_route("one.example.test")])).ok
        with pytest.raises(ConflictError, match="already equals"):
            await w.service.rollback(principal(), TENANT, 1)
        with pytest.raises(NotFoundError):
            await w.service.rollback(principal(), TENANT, 99)


async def test_two_applies_to_the_same_group_are_serialised(pg_url: str) -> None:
    async with world(pg_url) as w:
        inside = 0
        overlap = 0

        async def slow(engine: FakeHaproxy) -> None:
            nonlocal inside, overlap
            inside += 1
            overlap = max(overlap, inside)
            await asyncio.sleep(0.2)
            inside -= 1

        for engine in w.engines.values():
            engine.on_push = slow
        # two independent change sets on different objects, so neither is stale for the other
        one = await w.service.open(principal(), TENANT, "one")
        await w.service.add_ops(
            principal(), TENANT, one.record.id, [add_host_route("one.example.test")]
        )
        two = await w.service.open(principal(), TENANT, "two")
        await w.service.add_ops(
            principal(), TENANT, two.record.id, [update_pool(algorithm="leastconn")]
        )
        await w.service.apply(principal(), TENANT, one.record.id)
        await w.service.apply(principal(), TENANT, two.record.id)
        reports = await asyncio.gather(
            w.pipeline.run(TENANT, one.record.id), w.pipeline.run(TENANT, two.record.id)
        )
        assert overlap == 1, "pushes of two applies must never overlap"
        assert [r.ok for r in reports] == [True, True]
        assert sorted(r.revision_number or 0 for r in reports) == [1, 2]


async def test_each_other_groups_do_not_block(pg_url: str) -> None:
    from switchyard.pipeline.locks import PgGroupLocks

    async with world(pg_url) as w:
        locks = PgGroupLocks(w.db.admin, timeout_s=1, poll_s=0.05)
        other = uuid.UUID(int=12345)
        async with locks.hold([GROUP]):
            async with locks.hold([other]):  # a different group is free
                pass
            with pytest.raises(TimeoutError):
                async with locks.hold([GROUP]):
                    pass


async def test_tenant_scope_hides_other_tenants_change_sets(
    pg_url: str, app_engine: AsyncEngine
) -> None:
    async with world(pg_url) as w:
        detail = await w.service.open(principal(), TENANT, "mine")
        async with tenant_session(app_engine, [uuid.UUID(int=77)]) as s:
            rows = (await s.execute(text("SELECT count(*) FROM change_set"))).scalar_one()
        assert rows == 0
        assert detail.record.tenant_id == TENANT


async def test_reapply_repairs_drifted_engines_without_a_revision(pg_url: str) -> None:
    async with world(pg_url) as w:
        assert (await w.run_change([add_host_route("one.example.test")])).ok
        wanted = w.engines["se-a"].files["haproxy/haproxy.cfg"]
        # Someone hand-edits both engines.
        for engine in w.engines.values():
            engine.files["haproxy/haproxy.cfg"] = "# hotfix by hand\n" + wanted
        revisions = await scalar(w, "SELECT count(*) FROM revision")

        assert await w.service.reapply_group(principal(), TENANT, GROUP)
        report = await w.pipeline.reapply(TENANT, GROUP)

        assert report.ok, report.error
        assert report.revision_number == 1 and report.se_group
        assert all(e.files["haproxy/haproxy.cfg"] == wanted for e in w.engines.values())
        assert await scalar(w, "SELECT count(*) FROM revision") == revisions, "no new revision"
        deployments = await scalar(
            w, "SELECT count(*) FROM deployment WHERE change_set_id IS NULL AND state = 'succeeded'"
        )
        assert deployments == 1
        actions = await scalar(
            w, "SELECT count(*) FROM audit_event WHERE action LIKE 'se_group.reappl%'"
        )
        assert actions == 2, "requested and done"
        assert await scalar(w, "SELECT count(*) FROM job WHERE kind = 'reapply_se_group'") == 1


async def test_reapply_reports_a_failed_push_and_asks_for_the_right_role(pg_url: str) -> None:
    from switchyard.domain.workflow import Role
    from switchyard.services.errors import PermissionDeniedError

    async with world(pg_url) as w:
        assert (await w.run_change([add_host_route("one.example.test")])).ok
        with pytest.raises(PermissionDeniedError):
            await w.service.reapply_group(principal(Role.OPERATOR), TENANT, GROUP)
        with pytest.raises(NotFoundError):
            await w.service.reapply_group(principal(), TENANT, uuid.uuid4())

        from switchyard.adapters.haproxy import HaproxyError

        async def reject(engine: FakeHaproxy) -> None:
            raise HaproxyError("rejected")

        w.engines["se-a"].on_push = reject
        report = await w.pipeline.reapply(TENANT, GROUP)
        assert not report.ok and "rejected" in (report.error or "")
        assert await scalar(w, "SELECT count(*) FROM deployment WHERE state = 'failed'") == 1
        assert (
            await scalar(
                w, "SELECT count(*) FROM audit_event WHERE action = 'se_group.reapply_failed'"
            )
            == 1
        )
        with pytest.raises(PipelineStateError):
            await w.pipeline.reapply(TENANT, uuid.uuid4())

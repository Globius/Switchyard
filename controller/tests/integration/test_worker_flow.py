# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Jobs end to end: API-style apply, worker, apply pipeline, Git mirror; plus engines and members."""

import uuid

import pytest
from sqlalchemy import text

from switchyard.adapters.git import FakeGitMirror
from switchyard.db.tenancy import admin_session
from switchyard.domain.models import AdminState
from switchyard.domain.snapshot import Snapshot
from switchyard.domain.workflow import EngineState, Role
from switchyard.jobs.handlers import build_handlers
from switchyard.jobs.worker import Worker
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.engines import EngineService, validate_dataplane_url
from switchyard.services.errors import AlreadyExistsError, NotFoundError, PermissionDeniedError
from switchyard.services.members import MemberService
from tests.pipeline_env import (
    GROUP,
    POOL,
    TENANT,
    World,
    add_host_route,
    engine_record,
    principal,
    world,
)

pytestmark = pytest.mark.usefixtures("owner_engine")


async def scalar(w: World, sql: str, **params: object) -> object:
    async with admin_session(w.db.admin) as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def test_apply_job_then_git_mirror_job(pg_url: str) -> None:
    async with world(pg_url) as w:
        mirror = FakeGitMirror()
        worker = Worker(w.db.admin, build_handlers(w.db, w.pipeline, mirror), worker_id="t")
        detail = await w.service.open(principal(), TENANT, "add a route")
        await w.service.add_ops(
            principal(), TENANT, detail.record.id, [add_host_route("n.example.test")]
        )
        job_id = await w.service.apply(principal(), TENANT, detail.record.id, request_id="req-9")

        assert await worker.run_once()  # apply_change_set
        assert await scalar(w, "SELECT state FROM job WHERE id = :i", i=job_id) == "succeeded"
        assert await scalar(w, "SELECT result->>'ok' FROM job WHERE id = :i", i=job_id) == "true"
        assert await scalar(w, "SELECT state FROM change_set") == "applied"

        assert await worker.run_once()  # git_mirror
        sha = await scalar(w, "SELECT git_sha FROM revision WHERE number = 1")
        assert sha == mirror.commits[0][0]
        assert "tenants/acme/virtual_services/web.yaml" in mirror.tree
        assert "n.example.test" in mirror.tree["tenants/acme/virtual_services/web.yaml"]
        assert await scalar(w, "SELECT request_id FROM job WHERE kind = 'git_mirror'") == "req-9"
        assert not await worker.run_once()


async def test_failed_apply_is_a_finished_job_not_a_retry(pg_url: str) -> None:
    async with world(pg_url) as w:
        worker = Worker(w.db.admin, build_handlers(w.db, w.pipeline, None), worker_id="t")
        w.engines["se-a"].fail_next("rejected")
        detail = await w.service.open(principal(), TENANT, "will be rejected")
        await w.service.add_ops(
            principal(), TENANT, detail.record.id, [add_host_route("n.example.test")]
        )
        job_id = await w.service.apply(principal(), TENANT, detail.record.id)
        assert await worker.run_once()
        assert await scalar(w, "SELECT state FROM job WHERE id = :i", i=job_id) == "succeeded"
        assert await scalar(w, "SELECT result->>'ok' FROM job WHERE id = :i", i=job_id) == "false"
        assert await scalar(w, "SELECT attempts FROM job WHERE id = :i", i=job_id) == 1
        assert await scalar(w, "SELECT state FROM change_set") == "failed"


async def test_mirror_failure_never_affects_the_applied_change(pg_url: str) -> None:
    async with world(pg_url) as w:
        mirror = FakeGitMirror()
        worker = Worker(w.db.admin, build_handlers(w.db, w.pipeline, mirror), worker_id="t")
        assert (await w.run_change([add_host_route("n.example.test")])).ok
        mirror.fail_next = True
        assert await worker.run_once()  # the git_mirror job fails and is queued again
        assert await scalar(w, "SELECT state FROM job WHERE kind = 'git_mirror'") == "queued"
        assert await scalar(w, "SELECT state FROM change_set") == "applied"
        assert await scalar(w, "SELECT git_sha FROM revision WHERE number = 1") is None


class TestEngineRegistry:
    async def test_register_list_disable_remove(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            service = EngineService(w.db)
            admin = principal()
            record = await service.register(
                admin,
                TENANT,
                se_group_id=GROUP,
                name="se-1",
                dataplane_url="https://10.0.0.5:5556/",
                credentials_ref="kv/engines/se-1",
            )
            assert (record.dataplane_url, record.state) == (
                "https://10.0.0.5:5556",
                EngineState.ENABLED,
            )
            with pytest.raises(AlreadyExistsError):
                await service.register(
                    admin, TENANT, se_group_id=GROUP, name="se-1", dataplane_url="http://x:1"
                )
            assert [e.name for e in await service.list_engines(admin, TENANT, GROUP)] == ["se-1"]
            disabled = await service.set_state(admin, TENANT, record.id, EngineState.DISABLED)
            assert disabled.state is EngineState.DISABLED
            await service.remove(admin, TENANT, record.id)
            with pytest.raises(NotFoundError):
                await service.remove(admin, TENANT, record.id)

    async def test_validation_and_permissions(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            service = EngineService(w.db)
            with pytest.raises(PermissionDeniedError):
                await service.register(
                    principal(Role.OPERATOR),
                    TENANT,
                    se_group_id=GROUP,
                    name="se-1",
                    dataplane_url="http://x:1",
                )
            for bad_url in ("ftp://x", "http://u:p@x", "http://x/path", "x"):
                with pytest.raises(ValueError, match="dataplane_url"):
                    await service.register(
                        principal(), TENANT, se_group_id=GROUP, name="se-1", dataplane_url=bad_url
                    )
            with pytest.raises(ValueError):
                await service.register(
                    principal(),
                    TENANT,
                    se_group_id=GROUP,
                    name="Bad Name",
                    dataplane_url="http://x:1",
                )
            with pytest.raises(ValueError):
                await service.register(
                    principal(),
                    TENANT,
                    se_group_id=GROUP,
                    name="se-1",
                    dataplane_url="http://x:1",
                    credentials_ref="../secret",
                )
            with pytest.raises(NotFoundError):
                await service.register(
                    principal(),
                    TENANT,
                    se_group_id=uuid.UUID(int=5),
                    name="se-1",
                    dataplane_url="http://x:1",
                )

    def test_url_validation(self) -> None:
        assert validate_dataplane_url("http://a.b:80") == "http://a.b:80"


class TestMemberState:
    def member_id(self, snapshot: Snapshot) -> uuid.UUID:
        return snapshot.pools[0].members[0].id

    async def test_state_is_stored_and_applied_on_every_engine(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            directory = StaticEngineDirectory(
                {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
            )
            service = MemberService(w.db, directory)
            member = self.member_id(await w.snapshot())
            result = await service.set_state(
                principal(Role.OPERATOR), TENANT, POOL, member, AdminState.DRAIN
            )
            assert (result.applied, result.failed) == (["se-a", "se-b"], {})
            for engine in w.engines.values():
                assert [s.status for s in await engine.server_statuses()] == ["DRAIN"]
            pool = (await w.snapshot()).pools[0]
            assert (pool.members[0].admin_state, pool.version) == (AdminState.DRAIN, 2)
            assert (
                await scalar(
                    w, "SELECT count(*) FROM audit_event WHERE action = 'pool_member.state'"
                )
                == 1
            )
            assert await scalar(w, "SELECT count(*) FROM revision") == 0, (
                "no change set, no revision"
            )

    async def test_a_drain_with_a_timeout_ends_in_disabled(self, pg_url: str) -> None:
        from datetime import UTC, datetime, timedelta

        async with world(pg_url) as w:
            directory = StaticEngineDirectory(
                {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
            )
            service = MemberService(w.db, directory)
            member = self.member_id(await w.snapshot())
            async with admin_session(w.db.admin) as s:
                await s.execute(
                    text("UPDATE pool_member SET drain_timeout_s = 60 WHERE id = :i"), {"i": member}
                )
            await service.set_state(
                principal(Role.OPERATOR), TENANT, POOL, member, AdminState.DRAIN
            )
            now = datetime.now(UTC)
            assert await service.expire_drains(now=now + timedelta(seconds=30)) == 0
            assert (await w.snapshot()).pools[0].members[0].admin_state is AdminState.DRAIN
            assert await service.expire_drains(now=now + timedelta(seconds=90)) == 1
            assert (await w.snapshot()).pools[0].members[0].admin_state is AdminState.DISABLED
            for engine in w.engines.values():
                assert [st.status for st in await engine.server_statuses()] == ["MAINT"]
            assert await scalar(w, "SELECT drain_since FROM pool_member") is None
            assert await service.expire_drains(now=now + timedelta(days=1)) == 0

    async def test_a_drain_without_a_start_time_starts_its_clock(self, pg_url: str) -> None:
        from datetime import UTC, datetime

        async with world(pg_url) as w:
            service = MemberService(w.db, StaticEngineDirectory({}))
            async with admin_session(w.db.admin) as s:
                await s.execute(
                    text(
                        "UPDATE pool_member SET admin_state = 'drain', drain_timeout_s = 5,"
                        " drain_since = NULL"
                    )
                )
            assert await service.expire_drains(now=datetime.now(UTC)) == 0
            assert await scalar(w, "SELECT drain_since FROM pool_member") is not None

    async def test_unreachable_engines_are_reported_but_the_state_is_kept(
        self, pg_url: str
    ) -> None:
        async with world(pg_url) as w:
            from switchyard.adapters.haproxy import Unreachable

            async def broken(*_: object) -> None:
                raise Unreachable("se-b is down")

            w.engines["se-b"].set_server_state = broken  # type: ignore[method-assign,assignment]
            directory = StaticEngineDirectory(
                {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
            )
            result = await MemberService(w.db, directory).set_state(
                principal(Role.OPERATOR),
                TENANT,
                POOL,
                self.member_id(await w.snapshot()),
                AdminState.DISABLED,
            )
            assert result.applied == ["se-a"]
            assert "se-b is down" in result.failed["se-b"]
            assert (await w.snapshot()).pools[0].members[0].admin_state is AdminState.DISABLED

    async def test_permissions_and_unknown_members(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            directory = StaticEngineDirectory({GROUP: []})
            service = MemberService(w.db, directory)
            member = self.member_id(await w.snapshot())
            with pytest.raises(PermissionDeniedError):
                await service.set_state(
                    principal(Role.VIEWER), TENANT, POOL, member, AdminState.DRAIN
                )
            with pytest.raises(NotFoundError):
                await service.set_state(
                    principal(), TENANT, POOL, uuid.UUID(int=1), AdminState.DRAIN
                )

    async def test_the_next_render_reproduces_the_stored_state(self, pg_url: str) -> None:
        async with world(pg_url) as w:
            directory = StaticEngineDirectory(
                {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
            )
            await MemberService(w.db, directory).set_state(
                principal(), TENANT, POOL, self.member_id(await w.snapshot()), AdminState.DRAIN
            )
            assert (await w.run_change([add_host_route("n.example.test", expected=1)])).ok
            assert "weight 100 disabled" in w.engines["se-a"].files["haproxy/haproxy.cfg"]

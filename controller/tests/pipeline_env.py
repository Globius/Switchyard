"""A ready-made world for service and pipeline tests: tenant, SE group, services, fake engines."""

import copy
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.adapters.fake_haproxy import FakeHaproxy
from switchyard.db.repository import add_snapshot, load_snapshot
from switchyard.db.tenancy import ADMIN_ROLE, APP_ROLE, admin_session, make_engine, tenant_session
from switchyard.domain.changes import Action, ChangeOp, ObjectType
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.domain.snapshot import Snapshot
from switchyard.domain.workflow import EngineState, Role
from switchyard.pipeline.apply import ApplyPipeline
from switchyard.pipeline.engines import EngineRecord, StaticEngineDirectory
from switchyard.render.haproxy import render_haproxy
from switchyard.services.changesets import ChangeSetService
from switchyard.services.context import Databases, Principal
from tests.factories import base_data

TENANT = uuid.UUID(symbolic_uuid("t-acme"))
GROUP = uuid.UUID(symbolic_uuid("seg-1"))
POOL = uuid.UUID(symbolic_uuid("pool-web"))
VS = uuid.UUID(symbolic_uuid("vs-web"))


def principal(
    role: Role = Role.TENANT_ADMIN, name: str = "alice", tenant: uuid.UUID = TENANT
) -> Principal:
    return Principal(
        actor=f"user:{name}",
        role=role,
        tenant_ids=frozenset() if role is Role.SYSTEM_ADMIN else frozenset({tenant}),
    )


def world_data(**overrides: Any) -> dict[str, Any]:
    """Base data with a zero-second canary and a monitored pool, so reverts are observable."""
    data = copy.deepcopy(base_data())
    data["se_groups"][0]["canary_seconds"] = 0
    data.update(overrides)
    return data


def engine_record(name: str) -> EngineRecord:
    return EngineRecord(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, name),
        tenant_id=TENANT,
        se_group_id=GROUP,
        name=name,
        dataplane_url=f"http://{name}.invalid",
        credentials_ref=None,
        state=EngineState.ENABLED,
    )


@dataclass
class World:
    db: Databases
    engines: dict[str, FakeHaproxy] = field(default_factory=dict)
    service: ChangeSetService = field(init=False)
    pipeline: ApplyPipeline = field(init=False)

    def __post_init__(self) -> None:
        self.service = ChangeSetService(self.db)

    def use_engines(self, *names: str, states: dict[str, EngineState] | None = None) -> None:
        self.engines = {n: FakeHaproxy(n) for n in names}
        records = []
        for n in names:
            rec = engine_record(n)
            if states and n in states:
                rec = EngineRecord(**{**rec.__dict__, "state": states[n]})
            records.append((rec, self.engines[n]))

        directory = StaticEngineDirectory({GROUP: records})

        async def no_sleep(_: float) -> None:
            return None

        self.pipeline = ApplyPipeline(self.db, directory, sleep=no_sleep, lock_timeout_s=5)

    async def prime(self) -> None:
        """Make every fake engine run the current state, as a deployed SE group would."""
        rendered = render_haproxy(await self.snapshot(), GROUP)
        for engine in self.engines.values():
            await engine.push(rendered.files)
            engine.pushes.clear()

    async def snapshot(self) -> Snapshot:
        async with tenant_session(self.db.app, [TENANT]) as s:
            return await load_snapshot(s)

    async def run_change(
        self,
        ops: list[ChangeOp],
        who: Principal | None = None,
        *,
        summary: str = "test change",
    ) -> Any:
        """Open a change set, add ``ops``, queue it and run the pipeline. Returns the report."""
        who = who or principal()
        detail = await self.service.open(who, TENANT, summary)
        await self.service.add_ops(who, TENANT, detail.record.id, ops)
        await self.service.apply(who, TENANT, detail.record.id)
        return await self.pipeline.run(TENANT, detail.record.id)


def update_pool(**body: Any) -> ChangeOp:
    return ChangeOp(
        object_type=ObjectType.POOL,
        object_id=POOL,
        action=Action.UPDATE,
        body=body,
        expected_version=1,
    )


def add_host_route(host: str, *, expected: int = 1) -> ChangeOp:
    """Update the service so it also routes ``host`` to the existing pool (changes the render)."""
    return ChangeOp(
        object_type=ObjectType.VIRTUAL_SERVICE,
        object_id=VS,
        action=Action.UPDATE,
        body={
            "routes": [
                {"match": {"host": "app.example.test"}, "pool_id": str(POOL)},
                {"match": {"host": host}, "pool_id": str(POOL)},
            ]
        },
        expected_version=expected,
    )


@asynccontextmanager
async def world(pg_url: str, data: dict[str, Any] | None = None) -> AsyncIterator[World]:
    """Create engines, seed ``data`` and yield a :class:`World`; tables are emptied by the fixtures."""
    app = make_engine(pg_url, role=APP_ROLE)
    admin: AsyncEngine = make_engine(pg_url, role=ADMIN_ROLE)
    db = Databases(app=app, admin=admin)
    try:
        async with admin_session(admin) as s:
            await add_snapshot(s, snapshot_from_mapping(data or world_data()))
        w = World(db)
        w.use_engines("se-a", "se-b")
        await w.prime()
        yield w
    finally:
        await db.dispose()

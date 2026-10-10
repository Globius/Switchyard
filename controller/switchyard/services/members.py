"""Enable, disable or drain a pool member at runtime.

From the backend design: ``PUT /pools/{id}/members/{m}/state`` needs no change set and is audited.
The state is stored (so the next render reproduces it) and sent to every engine of every SE group
that uses the pool, through the Runtime API and without a reload.
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import update

from switchyard.adapters.haproxy import HaproxyError, ServerAdminState
from switchyard.db import audit_log
from switchyard.db.models import PoolMemberRow, PoolRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import tenant_session
from switchyard.domain.models import AdminState
from switchyard.pipeline.engines import EngineDirectory
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import NotFoundError, PermissionDeniedError

_RUNTIME: dict[AdminState, ServerAdminState] = {
    AdminState.ENABLED: "ready",
    AdminState.DISABLED: "maint",
    AdminState.DRAIN: "drain",
}


@dataclass(frozen=True)
class MemberStateResult:
    """What happened when a member's state changed.

    Attributes:
        state: The new stored state.
        applied: Engines that took the change at runtime.
        failed: Engines that could not be reached, by name, with the reason. The stored state is
            still the desired state; drift detection reports the mismatch.
    """

    state: AdminState
    applied: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


class MemberService:
    """Runtime control of pool members."""

    def __init__(self, db: Databases, engines: EngineDirectory) -> None:
        """Create the service."""
        self._db = db
        self._engines = engines

    async def set_state(
        self,
        principal: Principal,
        tenant_id: UUID,
        pool_id: UUID,
        member_id: UUID,
        state: AdminState,
        *,
        request_id: str | None = None,
    ) -> MemberStateResult:
        """Store ``state`` for the member and apply it on the engines serving the pool.

        Raises:
            NotFoundError: If the pool or member does not exist.
            PermissionDeniedError: If the caller lacks the ``operator`` role in this tenant.
        """
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, "set_state", "member")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
            pool = next((p for p in snapshot.pools if p.id == pool_id), None)
            member = next((m for m in pool.members if m.id == member_id), None) if pool else None
            if pool is None or member is None:
                raise NotFoundError("pool member does not exist")
            await session.execute(
                update(PoolMemberRow)
                .where(PoolMemberRow.id == member_id)
                .values(admin_state=state.value)
            )
            await session.execute(
                update(PoolRow).where(PoolRow.id == pool_id).values(version=PoolRow.version + 1)
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="pool_member.state",
                object_ref=f"pool:{pool.name}/{member.server_name}",
                diff={"from": member.admin_state.value, "to": state.value},
                request_id=request_id,
            )
            group_ids = {
                s.se_group_id
                for s in snapshot.virtual_services
                if s.enabled
                and (s.default_pool_id == pool_id or any(r.pool_id == pool_id for r in s.routes))
            }
        result = MemberStateResult(state=state)
        for group_id in sorted(group_ids):
            for record in await self._engines.list_engines(tenant_id, group_id):
                if record.state.value != "enabled":
                    continue
                engine = await self._engines.connect(record)
                try:
                    await engine.set_server_state(
                        f"pool_{pool.name}", member.server_name, _RUNTIME[state]
                    )
                    result.applied.append(record.name)
                except HaproxyError as exc:
                    result.failed[record.name] = str(exc)
                finally:
                    await engine.close()
        return result

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The real side effects of an engine roll, built on the autoscaler (ADR 0205).

A roll replaces an engine by doing what the autoscaler does to add one: deploy a staged guest
from the group's `autoscale.deploy` settings, write the enrollment files through the Proxmox guest
agent, enable it, reapply. Then it retires the old engine. So the group needs an autoscale policy
(`enabled` may be false) with `deploy` and `enroll` set, and the replacement must be a QEMU guest.
Not yet exercised against a real cluster (ADR 0205, "Not proven").
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select

from switchyard.db.models import SeGroupRow
from switchyard.db.ops_models import EngineStatusRow, JobRow, ServiceEngineRow
from switchyard.db.tenancy import admin_session
from switchyard.domain import roll as machine
from switchyard.domain.models import Autoscale
from switchyard.domain.workflow import EngineState, JobState
from switchyard.jobs import queue
from switchyard.pipeline.engines import EngineRecord
from switchyard.services.autoscale import Autoscaler
from switchyard.services.changesets import REAPPLY_JOB
from switchyard.services.context import Databases
from switchyard.services.health import HEALTHY_AT, engine_health, group_health
from switchyard.services.infra import DEPLOY_SE_GROUP_JOB, RETIRE_ENGINE_JOB, DeployRequest
from switchyard.services.roll import RollActionError, RollContext


class AutoscaleRollActions:
    """Roll actions over the same machinery the autoscaler uses."""

    def __init__(self, db: Databases, autoscaler: Autoscaler) -> None:
        """Create the actions."""
        self._db = db
        self._scaler = autoscaler

    async def _policy(self, ctx: RollContext) -> Autoscale:
        async with admin_session(self._db.admin) as session:
            group = await session.get(SeGroupRow, ctx.se_group_id)
        if group is None or group.autoscale is None:
            raise RollActionError(
                "the SE group has no autoscale settings; a roll needs autoscale.deploy and "
                "autoscale.enroll to create and enroll the replacements"
            )
        return Autoscale.model_validate(group.autoscale)

    async def _engine(self, ctx: RollContext, name: str) -> ServiceEngineRow | None:
        async with admin_session(self._db.admin) as session:
            return (
                await session.execute(
                    select(ServiceEngineRow).where(
                        ServiceEngineRow.se_group_id == ctx.se_group_id,
                        ServiceEngineRow.name == name,
                    )
                )
            ).scalar_one_or_none()

    async def _job(self, job_id: str | None) -> JobRow | None:
        if not job_id:
            return None
        async with admin_session(self._db.admin) as session:
            return await session.get(JobRow, UUID(job_id))

    async def _deploy(self, ctx: RollContext, jobs: dict[str, Any]) -> None:
        policy = await self._policy(ctx)
        async with admin_session(self._db.admin) as session:
            existing = (
                await session.execute(
                    select(ServiceEngineRow.id).where(
                        ServiceEngineRow.se_group_id == ctx.se_group_id
                    )
                )
            ).all()
            request = DeployRequest(
                tenant_id=ctx.tenant_id,
                se_group_id=ctx.se_group_id,
                count=len(existing) + 1,
                nodes=policy.deploy.nodes,
                template_vmid=ctx.template_vmid,
                mgmt_pool_id=policy.deploy.mgmt_pool_id,
                pool=policy.deploy.pool,
                credentials_ref=policy.deploy.credentials_ref,
                dataplane_port=policy.deploy.dataplane_port,
                agent_port=policy.deploy.agent_port,
                ssh_public_keys=policy.deploy.ssh_public_keys,
                nic_pools=dict(policy.deploy.nic_pools),
                staged=True,
            )
            job_id = await queue.enqueue(
                session,
                DEPLOY_SE_GROUP_JOB,
                request.as_payload(),
                tenant_id=ctx.tenant_id,
                max_attempts=1,
            )
        jobs["deploy_job"] = str(job_id)

    async def execute(
        self, ctx: RollContext, command: machine.Command, jobs: dict[str, Any]
    ) -> None:
        """Queue the deploy, reapply or retire job; enrollment is sent from ``observe``."""
        match command:
            case machine.Deploy():
                await self._deploy(ctx, jobs)
            case machine.IssueToken():
                jobs.pop("enrolled", None)  # observe sends the files, and retries until it works
            case machine.Reapply():
                async with admin_session(self._db.admin) as session:
                    job_id = await queue.enqueue(
                        session,
                        REAPPLY_JOB,
                        {"tenant_id": str(ctx.tenant_id), "se_group_id": str(ctx.se_group_id)},
                        tenant_id=ctx.tenant_id,
                        max_attempts=1,
                    )
                jobs["reapply_job"] = str(job_id)
            case machine.Retire(engine=name):
                row = await self._engine(ctx, name)
                if row is None:
                    return
                async with admin_session(self._db.admin) as session:
                    job_id = await queue.enqueue(
                        session,
                        RETIRE_ENGINE_JOB,
                        {"tenant_id": str(ctx.tenant_id), "engine_id": str(row.id)},
                        tenant_id=ctx.tenant_id,
                        max_attempts=1,
                    )
                jobs["retire_job"] = str(job_id)

    async def observe(
        self, ctx: RollContext, step: machine.EngineStep, jobs: dict[str, Any], now: float
    ) -> machine.Facts:
        """Look up what the current phase is waiting for."""
        match step.phase:
            case machine.Phase.DEPLOYING:
                job = await self._job(jobs.get("deploy_job"))
                if job is None or job.state in (JobState.QUEUED.value, JobState.RUNNING.value):
                    return machine.Facts(now)
                created = (job.result or {}).get("created") or []
                if job.state != JobState.SUCCEEDED.value or not created:
                    return machine.Facts(now, deploy_failed=True)
                return machine.Facts(now, deployed=str(created[-1]))
            case machine.Phase.AWAITING_ENROLLMENT:
                return await self._enrollment(ctx, step, jobs, now)
            case machine.Phase.REAPPLYING | machine.Phase.REAPPLYING_AFTER:
                job = await self._job(jobs.get("reapply_job"))
                if job is None or job.state in (JobState.QUEUED.value, JobState.RUNNING.value):
                    return machine.Facts(now)
                result = job.result or {}
                # A reapply job can end "succeeded" with ok false (an engine timed out): not applied
                if job.state != JobState.SUCCEEDED.value or result.get("ok") is False:
                    why = str(result.get("error") or job.last_error or "the job failed")
                    return machine.Facts(now, reapply_error=why)
                return machine.Facts(now, reapplied=True)
            case machine.Phase.SETTLING | machine.Phase.SETTLING_AFTER:
                return await self._health(ctx, step, now)
            case machine.Phase.RETIRING:
                return machine.Facts(now, retired=await self._engine(ctx, step.old) is None)
        return machine.Facts(now)

    async def _enrollment(
        self, ctx: RollContext, step: machine.EngineStep, jobs: dict[str, Any], now: float
    ) -> machine.Facts:
        row = await self._engine(ctx, step.new or "")
        if row is None:
            return machine.Facts(now)
        if not jobs.get("enrolled"):
            try:
                await self._scaler.enroll_engine(ctx.tenant_id, await self._policy(ctx), row)
                jobs["enrolled"] = True
                jobs.pop("enroll_error", None)
            except RollActionError:
                raise
            except Exception as exc:
                jobs["enroll_error"] = f"{type(exc).__name__}: {exc}"[:200]
                return machine.Facts(now)
        record = EngineRecord(
            id=row.id,
            tenant_id=ctx.tenant_id,
            se_group_id=ctx.se_group_id,
            name=row.name,
            dataplane_url=row.dataplane_url,
            credentials_ref=row.credentials_ref,
            state=EngineState(row.state),
            agent_url=row.agent_url,
            mgmt_ip=str(row.mgmt_ip).split("/")[0] if row.mgmt_ip else None,
        )
        if not await self._scaler.engine_answers(record):
            return machine.Facts(now)
        async with admin_session(self._db.admin) as session:
            fresh = await session.get(ServiceEngineRow, row.id)
            if fresh is not None:
                fresh.state = EngineState.ENABLED.value
        return machine.Facts(now, enrolled=True)

    async def _health(
        self, ctx: RollContext, step: machine.EngineStep, now: float
    ) -> machine.Facts:
        async with admin_session(self._db.admin) as session:
            rows = list(
                (
                    await session.execute(
                        select(EngineStatusRow).where(
                            EngineStatusRow.se_group_id == ctx.se_group_id
                        )
                    )
                ).scalars()
            )
        scores = {
            r.name: engine_health(
                {"reachable": r.reachable, "drift": r.drift, "members": r.members, "agent": r.agent}
            )
            for r in rows
        }
        if not scores:
            return machine.Facts(now)
        group = group_health(list(scores.values()))
        new = scores.get(step.new or "")
        smoke = new is not None and new.score >= HEALTHY_AT  # reachable, no drift, members up
        return machine.Facts(
            now,
            group_score=group.score,
            engine_scores_ok=all(h.score >= HEALTHY_AT for h in scores.values()),
            smoke_ok=smoke,
        )

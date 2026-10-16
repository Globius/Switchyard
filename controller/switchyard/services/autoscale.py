# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Automatic scale-out of SE groups (ADR 0194).

A group with an ``autoscale`` block is watched by the ``autoscale_se_groups`` job. Each run, per
group, one of these happens:

* **Decide** (:func:`decide`, a pure function): the group is *busy* when the reachable engines'
  open connections, as a percent of each engine's connection limit and averaged, are at
  ``scale_out_at`` or more. After ``sustain_s`` of that, and when the group is under
  ``max_engines``, healthy, out of its cooldown and not already adding an engine, the autoscaler
  queues a ``deploy_se_group`` job for one more engine, registered ``disabled`` (*staged*) so no
  rollout or poll touches an engine that cannot answer yet.
* **Enroll**: when that job has finished, the controller creates the engine's one-time enrollment
  token and hands the enrollment files to the guest through Proxmox's guest agent (the controller
  never uses SSH, and the guest agent call is the only way in).
* **Join**: once the new engine's agent answers, the engine is enabled and the group is reapplied
  (canary first, no revision), exactly the manual procedure of ``runbooks/rolling-upgrade.md``.

Nothing is ever removed: scale-in stays manual. A stuck step (a failed deploy, an engine that has
not enrolled after :data:`ENROLL_STALL`) is shown as ``blocked`` with the reason, and stops
further scale-out for that group until someone retires or fixes the engine.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from switchyard.adapters.agent import AgentError
from switchyard.adapters.proxmox import Proxmox, ProxmoxError
from switchyard.adapters.secrets import SecretNotFoundError, SecretsReader
from switchyard.db import audit_log
from switchyard.db.models import SeGroupRow
from switchyard.db.ops_models import (
    AutoscaleStateRow,
    EngineStatusRow,
    JobRow,
    ServiceEngineRow,
)
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.models import Autoscale
from switchyard.domain.workflow import EngineState, JobState, Role
from switchyard.jobs import queue
from switchyard.pipeline.engines import EngineDirectory, EngineRecord
from switchyard.services.changesets import REAPPLY_JOB, require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.enrollment import EnrollmentService
from switchyard.services.health import HEALTHY_AT, engine_health, group_health
from switchyard.services.infra import DEPLOY_SE_GROUP_JOB, DeployRequest

log = logging.getLogger("switchyard.autoscale")

AUTOSCALE_JOB = "autoscale_se_groups"
#: How long a new engine may take to enroll before the autoscaler reports it as stuck.
ENROLL_STALL = timedelta(minutes=20)
#: The identity the autoscaler acts as in the audit log and in the enrollment service.
SYSTEM = Principal(actor="system:autoscale", role=Role.SYSTEM_ADMIN)
AGENT_DIR = "/etc/switchyard/agent"


@dataclass(frozen=True)
class Decision:
    """What one look at a group concluded.

    Attributes:
        state: ``idle``, ``busy`` (over the threshold, waiting out ``sustain_s``), ``at_max``,
            ``cooldown``, ``blocked`` or ``scale_out`` (add an engine now).
        busy_since: When the group became busy, kept while it stays busy.
        reason: Why, for ``blocked`` and the waiting states.
    """

    state: str
    busy_since: datetime | None
    reason: str | None = None


def utilization(loads: list[tuple[int, int]]) -> int | None:
    """Average percent of the connection limit in use over ``(connections, limit)`` pairs.

    Example:
        >>> utilization([(70, 100), (90, 100)])
        80
        >>> utilization([]) is None
        True
    """
    pairs = [(c, m) for c, m in loads if m > 0]
    if not pairs:
        return None
    return round(sum(100 * c / m for c, m in pairs) / len(pairs))


def decide(
    policy: Autoscale,
    *,
    now: datetime,
    util: int | None,
    engines: int,
    group_score: int,
    busy_since: datetime | None,
    last_scaled_at: datetime | None,
) -> Decision:
    """Whether the group needs one more engine now.

    Example:
        >>> from datetime import UTC
        >>> from switchyard.domain.models import AutoscaleDeploy, AutoscaleEnroll
        >>> p = Autoscale(
        ...     deploy=AutoscaleDeploy(nodes=("pve1",), template_vmid=9000,
        ...         mgmt_pool_id=UUID(int=1), credentials_ref="kv/tenants/a/dp"),
        ...     enroll=AutoscaleEnroll(controller_url="https://c.example.test", ca_ref="kv/ca"))
        >>> t = datetime(2026, 1, 1, tzinfo=UTC)
        >>> decide(p, now=t, util=10, engines=2, group_score=100,
        ...        busy_since=None, last_scaled_at=None).state
        'idle'
    """
    if util is None or util < policy.scale_out_at:
        return Decision("idle", None)
    since = busy_since or now
    if now - since < timedelta(seconds=policy.sustain_s):
        return Decision("busy", since, f"{util}% for {int((now - since).total_seconds())} s")
    if engines >= policy.max_engines:
        return Decision("at_max", since, f"{engines} engines is the limit")
    if last_scaled_at is not None and now - last_scaled_at < timedelta(seconds=policy.cooldown_s):
        return Decision("cooldown", since)
    if group_score < HEALTHY_AT:
        return Decision(
            "blocked", since, f"group health {group_score} is below {HEALTHY_AT}: fix it first"
        )
    return Decision("scale_out", since, f"{util}% for {policy.sustain_s} s")


def enrollment_files(
    policy: Autoscale, *, token: str, data_plane_login: str, ca_pem: str
) -> dict[str, tuple[str, str]]:
    """The files that make a new engine enroll: ``path -> (content, mode)``.

    The same three files ``deploy/quickstart/enroll-engine.sh`` writes: the enrollment settings
    with the Data Plane login, the one-time token and the controller CA.

    Example:
        >>> from switchyard.domain.models import AutoscaleDeploy, AutoscaleEnroll
        >>> p = Autoscale(
        ...     deploy=AutoscaleDeploy(nodes=("pve1",), template_vmid=9000,
        ...         mgmt_pool_id=UUID(int=1), credentials_ref="kv/tenants/a/dp"),
        ...     enroll=AutoscaleEnroll(controller_url="https://c.example.test:8443",
        ...     ca_ref="kv/ca"))
        >>> files = enrollment_files(p, token="t", data_plane_login="u:p", ca_pem="PEM")
        >>> files[f"{AGENT_DIR}/enroll.token"][1]
        '600'
    """
    user, _, password = data_plane_login.partition(":")
    env = (
        f"CONTROLLER_URL={policy.enroll.controller_url.rstrip('/')}\n"
        f"CONTROLLER_CN={policy.enroll.controller_cn}\n"
        f"DP_USER={user}\nDP_PASSWORD={password}\n"
    )
    return {
        f"{AGENT_DIR}/enroll.env": (env, "600"),
        f"{AGENT_DIR}/enroll.token": (token + "\n", "600"),
        f"{AGENT_DIR}/controller-ca.crt": (ca_pem.rstrip("\n") + "\n", "644"),
    }


class Autoscaler:
    """Runs the autoscale loop for every group that has a policy."""

    def __init__(
        self,
        db: Databases,
        directory: EngineDirectory,
        proxmox: Proxmox,
        enrollment: EnrollmentService,
        secrets: SecretsReader,
        *,
        clock: Clock = utcnow,
    ) -> None:
        """Create the autoscaler; ``proxmox`` is the same client the deploy job uses."""
        self._db = db
        self._directory = directory
        self._pve = proxmox
        self._enrollment = enrollment
        self._secrets = secrets
        self._clock = clock

    async def list_states(self, principal: Principal, tenant_id: UUID) -> list[dict[str, Any]]:
        """Each autoscaled group's last decision (any role in the tenant)."""
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            groups = {
                g.id: g
                for g in (
                    await session.execute(
                        select(SeGroupRow).where(SeGroupRow.autoscale.isnot(None))
                    )
                ).scalars()
            }
            states = {
                s.se_group_id: s
                for s in (await session.execute(select(AutoscaleStateRow))).scalars()
            }
            counts: dict[UUID, int] = {}
            for e in (await session.execute(select(ServiceEngineRow))).scalars():
                counts[e.se_group_id] = counts.get(e.se_group_id, 0) + 1
        out = []
        for gid, group in sorted(groups.items(), key=lambda kv: kv[1].name):
            state = states.get(gid)
            if not group.autoscale:  # a stored JSON null: the group has no settings
                continue
            policy = Autoscale.model_validate(group.autoscale)
            out.append(
                {
                    "se_group": group.name,
                    "enabled": policy.enabled,
                    "engines": counts.get(gid, 0),
                    "max_engines": policy.max_engines,
                    "scale_out_at": policy.scale_out_at,
                    "utilization": state.utilization if state else None,
                    "decision": (state.decision if state else "idle") if policy.enabled else "off",
                    "reason": state.reason if state else None,
                    "last_scaled_at": state.last_scaled_at if state else None,
                }
            )
        return out

    async def run(self) -> dict[str, int]:
        """The scheduled job: look at every group with a policy; returns the counts."""
        totals = {"groups": 0, "scaled_out": 0, "joined": 0, "blocked": 0}
        async with admin_session(self._db.admin) as session:
            wanted = [
                (g.tenant_id, g.id, g.name, g.autoscale)
                for g in (
                    await session.execute(
                        select(SeGroupRow).where(SeGroupRow.autoscale.isnot(None))
                    )
                ).scalars()
            ]
        for tenant_id, group_id, name, raw in wanted:
            if not raw:  # JSON null stored by an older release counts as "no settings"
                continue
            try:
                outcome = await self._tick(tenant_id, group_id, name, Autoscale.model_validate(raw))
            except Exception:  # one group's trouble must not stop the others
                log.exception("autoscale failed for %s", group_id)
                continue
            totals["groups"] += 1
            for key in ("scaled_out", "joined", "blocked"):
                totals[key] += outcome == key
        return totals

    async def _state(self, session: Any, tenant_id: UUID, group_id: UUID) -> AutoscaleStateRow:
        state = await session.get(AutoscaleStateRow, group_id)
        if state is None:
            state = AutoscaleStateRow(se_group_id=group_id, tenant_id=tenant_id)
            session.add(state)
            await session.flush()
        return state  # type: ignore[no-any-return]

    async def _tick(self, tenant_id: UUID, group_id: UUID, name: str, policy: Autoscale) -> str:
        """One look at one group. Returns ``scaled_out``, ``joined``, ``blocked`` or ``""``."""
        now = self._clock()
        async with tenant_session(self._db.app, [tenant_id]) as session:
            state = await self._state(session, tenant_id, group_id)
            state.updated_at = now
            if not policy.enabled:
                state.decision, state.reason, state.busy_since = "off", None, None
                return ""
            if state.pending_job_id is not None or state.pending_engine_id is not None:
                return await self._advance(session, tenant_id, group_id, name, policy, state, now)
            rows = list((await session.execute(select(EngineStatusRow))).scalars())
            engines = list((await session.execute(select(ServiceEngineRow))).scalars())
            members = [e for e in engines if e.se_group_id == group_id]
            statuses = [r for r in rows if r.se_group_id == group_id]
            loads = [
                (int(r.agent["load"]["conns"]), int(r.agent["load"]["max_conn"]))
                for r in statuses
                if r.reachable and r.agent and r.agent.get("load")
            ]
            util = utilization(loads)
            score = group_health(
                [
                    engine_health(
                        {
                            "reachable": r.reachable,
                            "drift": r.drift,
                            "members": r.members,
                            "agent": r.agent,
                        }
                    )
                    for r in statuses
                ]
            ).score
            decision = decide(
                policy,
                now=now,
                util=util,
                engines=len(members),
                group_score=score,
                busy_since=state.busy_since,
                last_scaled_at=state.last_scaled_at,
            )
            state.utilization, state.busy_since = util, decision.busy_since
            state.decision, state.reason = decision.state, decision.reason
            if decision.state == "blocked":
                return "blocked"
            if decision.state != "scale_out":
                return ""
            request = DeployRequest(
                tenant_id=tenant_id,
                se_group_id=group_id,
                count=len(members) + 1,
                nodes=policy.deploy.nodes,
                template_vmid=policy.deploy.template_vmid,
                mgmt_pool_id=policy.deploy.mgmt_pool_id,
                pool=policy.deploy.pool,
                credentials_ref=policy.deploy.credentials_ref,
                dataplane_port=policy.deploy.dataplane_port,
                agent_port=policy.deploy.agent_port,
                ssh_public_keys=policy.deploy.ssh_public_keys,
                nic_pools=dict(policy.deploy.nic_pools),
                staged=True,
            )
            state.pending_job_id = await queue.enqueue(
                session,
                DEPLOY_SE_GROUP_JOB,
                request.as_payload(),
                tenant_id=tenant_id,
                max_attempts=1,
            )
            state.pending_since, state.enroll_sent_at, state.pending_engine_id = now, None, None
            state.decision, state.reason = "scaling", f"deploying engine {len(members) + 1}"
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=SYSTEM.actor,
                action="autoscale.scale_out",
                object_ref=f"se_group:{name}",
                diff={"utilization": util, "engines": len(members), "to": len(members) + 1},
            )
            return "scaled_out"

    async def _advance(
        self,
        session: Any,
        tenant_id: UUID,
        group_id: UUID,
        name: str,
        policy: Autoscale,
        state: AutoscaleStateRow,
        now: datetime,
    ) -> str:
        """Move an engine that is being added one step further."""
        if state.pending_engine_id is None:
            job = await session.get(JobRow, state.pending_job_id)
            if job is None or job.state in (JobState.QUEUED.value, JobState.RUNNING.value):
                state.decision = "scaling"
                return self._stalled(state, now)
            created = (job.result or {}).get("created") or []
            if job.state != JobState.SUCCEEDED.value or not created:
                reason = f"the deploy job {job.state}: {job.last_error or 'no engine created'}"
                return self._give_up(state, now, reason)
            engine = (
                await session.execute(
                    select(ServiceEngineRow).where(
                        ServiceEngineRow.se_group_id == group_id,
                        ServiceEngineRow.name == created[-1],
                    )
                )
            ).scalar_one_or_none()
            if engine is None:
                return self._give_up(state, now, f"engine {created[-1]} is not registered")
            state.pending_engine_id = engine.id
        engine = await session.get(ServiceEngineRow, state.pending_engine_id)
        if engine is None:
            return self._give_up(state, now, "the new engine was removed")
        record = EngineRecord(
            id=engine.id,
            tenant_id=tenant_id,
            se_group_id=group_id,
            name=engine.name,
            dataplane_url=engine.dataplane_url,
            credentials_ref=engine.credentials_ref,
            state=EngineState(engine.state),
            agent_url=engine.agent_url,
            mgmt_ip=str(engine.mgmt_ip).split("/")[0] if engine.mgmt_ip else None,
        )
        if await self._answers(record):
            engine.state = EngineState.ENABLED.value
            await queue.enqueue(
                session,
                REAPPLY_JOB,
                {"tenant_id": str(tenant_id), "se_group_id": str(group_id)},
                tenant_id=tenant_id,
                max_attempts=1,
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=SYSTEM.actor,
                action="autoscale.engine_joined",
                object_ref=f"engine:{engine.name}",
            )
            state.pending_job_id = state.pending_engine_id = state.pending_since = None
            state.enroll_sent_at = None
            state.last_scaled_at, state.busy_since = now, None
            state.decision, state.reason = "cooldown", f"{engine.name} joined the group"
            return "joined"
        if state.enroll_sent_at is None:
            try:
                await self._enroll(tenant_id, policy, engine)
                state.enroll_sent_at = now
                state.reason = f"enrollment of {engine.name} sent"
            except (ProxmoxError, SecretNotFoundError, ValueError) as exc:
                state.reason = f"waiting to enroll {engine.name}: {exc}"
        state.decision = "scaling"
        return self._stalled(state, now)

    @staticmethod
    def _stalled(state: AutoscaleStateRow, now: datetime) -> str:
        if state.pending_since is not None and now - state.pending_since > ENROLL_STALL:
            reason = f"no new engine after {int(ENROLL_STALL.total_seconds() // 60)} minutes"
            if state.reason:
                reason += f" ({state.reason})"
            state.decision, state.reason = "blocked", reason
            return "blocked"
        return ""

    @staticmethod
    def _give_up(state: AutoscaleStateRow, now: datetime, reason: str) -> str:
        """Stop adding the engine, apply the cooldown and say why."""
        state.pending_job_id = state.pending_engine_id = state.pending_since = None
        state.enroll_sent_at = None
        state.last_scaled_at = now
        state.decision, state.reason = "blocked", reason
        return "blocked"

    async def _answers(self, record: EngineRecord) -> bool:
        """True once the engine's agent answers a status call (it has enrolled)."""
        agent = None
        try:
            agent = await self._directory.connect_agent(record)
            if agent is None:
                return False
            await agent.status()
            return True
        except (AgentError, OSError):
            return False
        finally:
            if agent is not None:
                await agent.close()

    async def _enroll(self, tenant_id: UUID, policy: Autoscale, engine: ServiceEngineRow) -> None:
        """Create the one-time token and write the enrollment files into the guest."""
        if engine.pve_node is None or engine.vmid is None:
            raise ValueError("the engine has no Proxmox guest")
        if engine.guest_kind not in (None, "qemu"):
            raise ValueError("only QEMU engines can be enrolled through the guest agent")
        login = await self._secrets.get(policy.deploy.credentials_ref)
        ca_pem = await self._secrets.get(policy.enroll.ca_ref)
        token, _ = await self._enrollment.create_token(SYSTEM, tenant_id, engine.id)
        files = enrollment_files(policy, token=token, data_plane_login=login, ca_pem=ca_pem)
        for path, (content, mode) in files.items():
            await self._pve.guest_write_file(engine.pve_node, engine.vmid, path, content)
            await self._pve.guest_exec(engine.pve_node, engine.vmid, ["chmod", mode, path])
        await self._pve.guest_exec(
            engine.pve_node, engine.vmid, ["systemctl", "start", "sy-agent-enroll.service"]
        )

    async def enroll_engine(
        self, tenant_id: UUID, policy: Autoscale, engine: ServiceEngineRow
    ) -> None:
        """Write the enrollment files into the guest of ``engine`` (also used by engine rolls)."""
        await self._enroll(tenant_id, policy, engine)

    async def engine_answers(self, record: EngineRecord) -> bool:
        """Whether the agent of ``record`` answers a status call, so it has enrolled."""
        return await self._answers(record)

"""The apply pipeline: render, validate, canary, roll out, commit.

Stages, in order (backend design, "Change pipeline"):

1. **Plan.** Apply the change set's operations to the latest state in memory, validate the result,
   and render every SE group that references changed objects. Groups whose rendered files do not
   change are left alone.
2. **Lock.** Take a PostgreSQL advisory lock per affected SE group, so two applies never deploy to
   the same group at once. State is read again under the locks.
3. **Validate.** The optional sandbox (``haproxy -c`` in a container) and the canary engine's own
   HAProxy (Data Plane API ``only_validate``). Nothing has changed on any engine yet.
4. **Canary.** Push to the first enabled engine of the group and watch it for the group's
   ``canary_seconds``. A failure reverts the canary before any other engine sees the change.
5. **Roll out.** Push to the group's remaining engines one at a time.
6. **Commit.** In one database transaction: write the changes (version-checked), the revision, the
   deployment rows and the audit event, and queue the Git mirror job.

Any failure after the first push reverts every engine that took the new configuration, marks the
change set ``failed`` and records what happened. Reverting restores the previous *rendered* files;
if an engine had no Switchyard-managed configuration before (a brand-new SE group), there is nothing
to restore and the report says so.
"""

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from switchyard.adapters.agent import AgentEngine, AgentError
from switchyard.adapters.haproxy import HaproxyEngine, HaproxyError, ServerStatus
from switchyard.adapters.sandbox import SandboxValidator, require_valid
from switchyard.adapters.secrets import SecretsReader
from switchyard.config import broker_url
from switchyard.db import audit_log
from switchyard.db import changesets as store
from switchyard.db.repository import load_snapshot
from switchyard.db.store import ConcurrentModificationError, apply_delta
from switchyard.db.tenancy import tenant_session
from switchyard.domain import models as dm
from switchyard.domain.changes import ChangeError, Draft, ObjectType, apply_ops
from switchyard.domain.errors import DomainValidationError
from switchyard.domain.models import SeGroup
from switchyard.domain.snapshot import Snapshot
from switchyard.domain.workflow import ChangeSetState, DeploymentState
from switchyard.jobs import queue
from switchyard.jobs.progress import APPLY_STAGES, REAPPLY_STAGES, JobProgress
from switchyard.pipeline.bundles import BundleError, engine_files
from switchyard.pipeline.canary import (
    AgentRoutingProbe,
    NoRoutingProbe,
    RoutingProbe,
    Sleep,
    canary_check,
)
from switchyard.pipeline.engines import EngineDirectory, EngineRecord
from switchyard.pipeline.locks import PgGroupLocks
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import RenderResult, render_haproxy
from switchyard.render.routing import EngineSlot
from switchyard.services.context import Clock, Databases, utcnow

log = logging.getLogger("switchyard.pipeline")


@dataclass(frozen=True)
class ReapplyReport:
    """Result of pushing the current desired state to one SE group again.

    Attributes:
        ok: True if every engine of the group now runs the desired configuration.
        se_group_id: The group.
        se_group: Its name.
        revision_number: The revision whose state was pushed (the tenant's latest).
        error: Why it failed.
        details: Per-engine notes (canary, bundles, what was reverted).
    """

    ok: bool
    se_group_id: UUID
    se_group: str
    revision_number: int
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """JSON form stored as the job result."""
        return {
            "ok": self.ok,
            "se_group_id": str(self.se_group_id),
            "se_group": self.se_group,
            "revision_number": self.revision_number,
            "error": self.error,
            "details": self.details,
        }


class PipelineStateError(RuntimeError):
    """The change set is not in the state the pipeline needs (it is not ``applying``)."""


@dataclass(frozen=True)
class GroupOutcome:
    """What happened to one SE group.

    Attributes:
        se_group_id: The group.
        se_group_name: Its name.
        state: ``succeeded``, ``failed`` or ``reverted``.
        rendered_hash: ``content_hash`` of the files that were pushed.
        fingerprint: Render fingerprint of the files that were pushed.
        error: Why it failed, if it did.
        details: Per-engine notes (pushed, reverted, canary notes).
    """

    se_group_id: UUID
    se_group_name: str
    state: DeploymentState
    rendered_hash: str
    fingerprint: str
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplyReport:
    """Result of running the pipeline for one change set.

    Attributes:
        ok: True if the change set was applied.
        change_set_id: The change set.
        revision_number: The new revision, when one was created.
        error: Why it failed.
        groups: Per-SE-group outcomes.
    """

    ok: bool
    change_set_id: UUID
    revision_number: int | None = None
    error: str | None = None
    groups: tuple[GroupOutcome, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """JSON form stored as the job result."""
        return {
            "ok": self.ok,
            "change_set_id": str(self.change_set_id),
            "revision_number": self.revision_number,
            "error": self.error,
            "groups": [
                {
                    "se_group_id": str(g.se_group_id),
                    "se_group": g.se_group_name,
                    "state": g.state.value,
                    "rendered_hash": g.rendered_hash,
                    "error": g.error,
                    "details": g.details,
                }
                for g in self.groups
            ],
        }


@dataclass
class _Plan:
    group: SeGroup
    target: RenderResult
    previous: RenderResult | None
    engines: list[tuple[EngineRecord, HaproxyEngine]] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)
    agents: dict[str, AgentEngine] = field(default_factory=dict)
    bundles: dict[str, dict[str, str]] = field(default_factory=dict)
    previous_bundles: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Engines whose bundle was already sent (certificates go ahead of the canary's validation).
    bundle_sent: set[str] = field(default_factory=set)

    @property
    def changed(self) -> bool:
        """True if the HAProxy files or any engine's routing bundle differ from before."""
        if self.previous is None or self.previous.content_hash != self.target.content_hash:
            return True
        return self.bundles != self.previous_bundles


class _RolloutFailedError(Exception):
    def __init__(self, plan: _Plan, message: str) -> None:
        super().__init__(message)
        self.plan = plan


class ApplyPipeline:
    """Runs the stages above for one change set."""

    def __init__(
        self,
        db: Databases,
        engines: EngineDirectory,
        *,
        sandbox: SandboxValidator | None = None,
        probe: RoutingProbe | None = None,
        secrets: SecretsReader | None = None,
        lock_timeout_s: float = 300.0,
        canary_poll_s: float = 2.0,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = utcnow,
    ) -> None:
        """Create the pipeline.

        Args:
            db: Database handles.
            engines: Finds and connects to each group's engines.
            sandbox: ``haproxy -c`` sandbox; skipped when ``None`` (the canary engine still
                validates with its own HAProxy).
            probe: Routing check for engines without an agent; defaults to "not checked".
            secrets: Resolves BGP session passwords for the routing renderer.
            lock_timeout_s: How long to wait for another apply on the same SE group.
            canary_poll_s: Delay between canary polls.
            sleep: Injected for tests.
            clock: Injected for tests.
        """
        self._db = db
        self._directory = engines
        self._sandbox = sandbox
        self._probe = probe or NoRoutingProbe()
        self._secrets = secrets
        self._locks = PgGroupLocks(db.admin, timeout_s=lock_timeout_s)
        self._poll = canary_poll_s
        self._sleep = sleep
        self._clock = clock

    # -- entry point -----------------------------------------------------------------------------

    async def run(
        self,
        tenant_id: UUID,
        change_set_id: UUID,
        *,
        request_id: str | None = None,
        progress: JobProgress | None = None,
    ) -> ApplyReport:
        """Apply a change set that is in the ``applying`` state.

        ``progress`` receives the stages (lock, render, validate, canary, rollout, commit) with
        their timing and errors; the worker passes the recorder of the job running the pipeline.

        Raises:
            PipelineStateError: If the change set does not exist or is not ``applying``.
        """
        started = self._clock()
        rec = progress or JobProgress(None, None)
        await rec.plan(APPLY_STAGES)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            change_set = await store.get_change_set(session, change_set_id)
            if change_set is None or change_set.state is not ChangeSetState.APPLYING:
                raise PipelineStateError(f"change set {change_set_id} is not being applied")
            ops = await store.load_ops(session, change_set_id)

        opened: list[HaproxyEngine | AgentEngine] = []
        try:
            for _attempt in range(3):
                base = await self._load_base(tenant_id)
                try:
                    async with rec.stage("render"):
                        draft = apply_ops(base, ops, tenant_id=tenant_id)
                        self._require_valid(draft)
                        plans = await self._plan(tenant_id, base, draft, opened)
                except (ChangeError, DomainValidationError, RenderError, _PlanError) as exc:
                    return await self._fail(
                        tenant_id, change_set_id, str(exc), [], request_id, started
                    )
                async with AsyncExitStack() as stack:
                    async with rec.stage("lock"):
                        await stack.enter_async_context(self._locks.hold(p.group.id for p in plans))
                    if await self._load_base(tenant_id) != base:
                        continue  # state moved while waiting for the locks; plan again
                    return await self._execute(
                        tenant_id, change_set_id, base, draft, plans, request_id, started, rec
                    )
            return await self._fail(
                tenant_id,
                change_set_id,
                "the tenant's state kept changing while locks were acquired; try again",
                [],
                request_id,
                started,
            )
        finally:
            for engine in opened:
                await engine.close()
            await rec.skip_pending()

    async def reapply(
        self,
        tenant_id: UUID,
        se_group_id: UUID,
        *,
        request_id: str | None = None,
        progress: JobProgress | None = None,
    ) -> ReapplyReport:
        """Push the tenant's current desired state to one SE group again (drift repair).

        Drift is reported, never corrected on its own (CLAUDE.md); this runs only when a person asks
        for it. It goes through the same stages as an apply, canary first, but makes no revision:
        the state in the database does not change. What the engines held before is unknown (it
        drifted), so there is nothing to revert to: a rejected push leaves an engine on what it ran.

        Raises:
            PipelineStateError: If the SE group does not exist.
        """
        started = self._clock()
        rec = progress or JobProgress(None, None)
        await rec.plan(REAPPLY_STAGES)
        opened: list[HaproxyEngine | AgentEngine] = []
        try:
            for _attempt in range(3):
                base = await self._load_base(tenant_id)
                group = next((g for g in base.se_groups if g.id == se_group_id), None)
                if group is None:
                    raise PipelineStateError(f"SE group {se_group_id} does not exist")
                async with tenant_session(self._db.app, [tenant_id]) as session:
                    revision_number = await store.current_revision(session, tenant_id)
                try:
                    async with rec.stage("render"):
                        plan = await self._plan_reapply(tenant_id, base, group, opened)
                except (RenderError, _PlanError) as exc:
                    return await self._reapply_done(
                        tenant_id, group, revision_number, started, request_id, error=str(exc)
                    )
                async with AsyncExitStack() as stack:
                    async with rec.stage("lock"):
                        await stack.enter_async_context(self._locks.hold([group.id]))
                    if await self._load_base(tenant_id) != base:
                        continue
                    pushed: list[tuple[HaproxyEngine, _Plan]] = []
                    try:
                        await self._rollout(plan, pushed, rec)
                    except _RolloutFailedError as exc:
                        await self._revert(pushed)
                        return await self._reapply_done(
                            tenant_id,
                            group,
                            revision_number,
                            started,
                            request_id,
                            error=str(exc),
                            plan=plan,
                        )
                    except Exception as exc:
                        log.exception("unexpected error while re-applying group %s", group.name)
                        return await self._reapply_done(
                            tenant_id,
                            group,
                            revision_number,
                            started,
                            request_id,
                            error=f"unexpected {type(exc).__name__}: {exc}",
                            plan=plan,
                        )
                    return await self._reapply_done(
                        tenant_id, group, revision_number, started, request_id, plan=plan
                    )
            raise PipelineStateError("the tenant's state kept changing while locks were acquired")
        finally:
            for engine in opened:
                await engine.close()
            await rec.skip_pending()

    async def _plan_reapply(
        self,
        tenant_id: UUID,
        base: Snapshot,
        group: SeGroup,
        opened: list[HaproxyEngine | AgentEngine],
    ) -> _Plan:
        target = render_haproxy(base, group.id, broker_url())
        plan = _Plan(group=group, target=target, previous=None)
        records = [
            r
            for r in await self._directory.list_engines(tenant_id, group.id)
            if r.state.value == "enabled"
        ]
        if not records:
            raise _PlanError(f"SE group {group.name} has no enabled engines")
        for record in records:
            engine = await self._directory.connect(record)
            opened.append(engine)
            plan.engines.append((record, engine))
            agent = await self._directory.connect_agent(record)
            if agent is not None:
                opened.append(agent)
                plan.agents[record.name] = agent
        await self._plan_bundles(plan, base, base, records)
        return plan

    async def _reapply_done(
        self,
        tenant_id: UUID,
        group: SeGroup,
        revision_number: int,
        started: datetime,
        request_id: str | None,
        *,
        error: str | None = None,
        plan: _Plan | None = None,
    ) -> ReapplyReport:
        """Record the outcome as a deployment and an audit event, and build the report."""
        state = DeploymentState.FAILED if error else DeploymentState.SUCCEEDED
        details = dict(plan.notes) if plan is not None else {}
        details["reapply"] = True
        async with tenant_session(self._db.app, [tenant_id]) as session:
            revision = await store.get_revision(session, tenant_id, revision_number)
            await store.insert_deployment(
                session,
                tenant_id=tenant_id,
                change_set_id=None,
                revision_id=revision.id if revision else None,
                se_group_id=group.id,
                state=state,
                rendered_hash=plan.target.content_hash if plan else None,
                fingerprint=plan.target.fingerprint if plan else None,
                error=error,
                details=details,
                started_at=started,
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor="system:reapply",
                action="se_group.reapply_failed" if error else "se_group.reapplied",
                object_ref=f"se_group:{group.name}",
                diff={"revision": revision_number, "error": error}
                if error
                else {"revision": revision_number},
                request_id=request_id,
            )
        return ReapplyReport(
            ok=error is None,
            se_group_id=group.id,
            se_group=group.name,
            revision_number=revision_number,
            error=error,
            details=details,
        )

    # -- planning --------------------------------------------------------------------------------

    async def _load_base(self, tenant_id: UUID) -> Snapshot:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            return await load_snapshot(session)

    @staticmethod
    def _require_valid(draft: Draft) -> None:
        if draft.problems:
            raise DomainValidationError(list(draft.problems))

    async def _plan(
        self,
        tenant_id: UUID,
        base: Snapshot,
        draft: Draft,
        opened: list[HaproxyEngine | AgentEngine],
    ) -> list[_Plan]:
        plans: list[_Plan] = []
        for group in sorted(draft.snapshot.se_groups, key=lambda g: g.name):
            target = render_haproxy(draft.snapshot, group.id, broker_url())
            existed = any(g.id == group.id for g in base.se_groups)
            previous: RenderResult | None = (
                render_haproxy(base, group.id, broker_url()) if existed else None
            )
            if not _renders_anything(target) and previous is None:
                continue  # a brand-new group with no services needs no push yet
            plan = _Plan(group=group, target=target, previous=previous)
            records = [
                r
                for r in await self._directory.list_engines(tenant_id, group.id)
                if r.state.value == "enabled"
            ]
            for record in records:
                engine = await self._directory.connect(record)
                opened.append(engine)
                plan.engines.append((record, engine))
                agent = await self._directory.connect_agent(record)
                if agent is not None:
                    opened.append(agent)
                    plan.agents[record.name] = agent
            await self._plan_bundles(plan, base, draft.snapshot, records)
            if not plan.changed:
                continue
            if not records:
                raise _PlanError(f"SE group {group.name} has no enabled engines to deploy to")
            plans.append(plan)
        return plans

    async def _plan_bundles(
        self, plan: _Plan, base: Snapshot, after: Snapshot, records: list[EngineRecord]
    ) -> None:
        """Build each agent-managed engine's files (routing and certificates), before and after."""
        group = plan.group
        if not _renders_anything(plan.target):
            return
        existed = plan.previous is not None
        for index, record in enumerate(records):
            if group.ha_mode is dm.HaMode.BGP and group.routing_profile_id and not record.mgmt_ip:
                raise _PlanError(
                    f"engine {record.name} needs mgmt_ip: it is used as the BGP router id"
                )
            if record.name not in plan.agents:
                continue  # no agent: routing and certificates are provisioned out of band
            slot = EngineSlot(record.name, index, record.mgmt_ip or "0.0.0.0")  # noqa: S104
            try:
                files = await engine_files(after, group, slot, self._secrets)
            except BundleError as exc:
                raise _PlanError(str(exc)) from exc
            if not files:
                continue
            plan.bundles[record.name] = files
            if existed:
                with contextlib.suppress(BundleError):
                    plan.previous_bundles[record.name] = await engine_files(
                        base, group, slot, self._secrets
                    )

    # -- execution -------------------------------------------------------------------------------

    async def _execute(
        self,
        tenant_id: UUID,
        change_set_id: UUID,
        base: Snapshot,
        draft: Draft,
        plans: list[_Plan],
        request_id: str | None,
        started: datetime,
        rec: JobProgress,
    ) -> ApplyReport:
        pushed: list[tuple[HaproxyEngine, _Plan]] = []
        outcomes: list[GroupOutcome] = []
        try:
            for plan in plans:
                await self._rollout(plan, pushed, rec)
                outcomes.append(_outcome(plan, DeploymentState.SUCCEEDED))
        except _RolloutFailedError as exc:
            reverted = await self._revert(pushed)
            failed = [
                _outcome(
                    p,
                    DeploymentState.FAILED if p is exc.plan else DeploymentState.REVERTED,
                    error=str(exc) if p is exc.plan else None,
                    details={**p.notes, "reverted": reverted.get(p.group.id, [])},
                )
                for p in plans
                if p is exc.plan or any(pp is p for _, pp in pushed)
            ]
            return await self._fail(tenant_id, change_set_id, str(exc), failed, request_id, started)
        except Exception as exc:
            # Whatever went wrong, no engine may be left on a configuration nobody committed.
            log.exception("unexpected error while rolling out change set %s", change_set_id)
            await self._revert(pushed)
            message = (
                f"unexpected {type(exc).__name__} during rollout; engines were reverted: {exc}"
            )
            return await self._fail(tenant_id, change_set_id, message, [], request_id, started)
        try:
            async with rec.stage("commit"):
                return await self._commit(
                    tenant_id, change_set_id, base, draft, plans, outcomes, request_id, started
                )
        except (ConcurrentModificationError, IntegrityError) as exc:
            reverted = await self._revert(pushed)
            message = f"the database changed during the rollout ({exc}); engines were reverted"
            failed = [
                _outcome(
                    p, DeploymentState.REVERTED, details={"reverted": reverted.get(p.group.id, [])}
                )
                for p in plans
            ]
            return await self._fail(tenant_id, change_set_id, message, failed, request_id, started)

    async def _push_one(self, plan: _Plan, record: EngineRecord, engine: HaproxyEngine) -> None:
        """Push HAProxy files and the agent bundle to one engine.

        When certificates changed they go first, so HAProxy never loads a config that names a
        certificate file that is not there yet; otherwise HAProxy goes first.
        """
        previous_files = plan.previous.files if plan.previous else None
        agent = plan.agents.get(record.name)
        bundle = plan.bundles.get(record.name)
        certs_changed = self._certs_changed(plan, record)
        if certs_changed and record.name not in plan.bundle_sent:
            await self._send_bundle(record, agent, bundle)
            plan.bundle_sent.add(record.name)
        await engine.push(plan.target.files, previous=previous_files)
        if not certs_changed:
            await self._send_bundle(record, agent, bundle)

    @staticmethod
    def _certs_changed(plan: _Plan, record: EngineRecord) -> bool:
        """True when this engine's bundle carries a certificate it does not have yet."""
        bundle = plan.bundles.get(record.name)
        before = plan.previous_bundles.get(record.name, {})
        return bundle is not None and any(
            path.startswith("certs/") and before.get(path) != text for path, text in bundle.items()
        )

    @staticmethod
    async def _send_bundle(
        record: EngineRecord, agent: AgentEngine | None, bundle: dict[str, str] | None
    ) -> None:
        if agent is None or bundle is None:
            return
        result = await agent.apply_bundle(bundle_id(bundle), bundle)
        if not result.ok:
            note = " (previous files restored)" if result.restored_previous else ""
            raise HaproxyError(
                f"{record.name}: agent rejected the routing bundle: {result.error}{note}"
            )

    async def _rollout(
        self, plan: _Plan, pushed: list[tuple[HaproxyEngine, _Plan]], rec: JobProgress
    ) -> None:
        """Validate, push to the canary, watch it, then push to the rest."""
        group, files = plan.group, plan.target.files
        canary_record, canary = plan.engines[0]
        try:
            async with rec.stage("validate"):
                if self._sandbox is not None:
                    # the certificates sit beside the config on a real engine, so they do here
                    certs = {
                        path: text
                        for path, text in plan.bundles.get(canary_record.name, {}).items()
                        if path.startswith("certs/")
                    }
                    await require_valid(self._sandbox, {**files, **certs})
                if self._certs_changed(plan, canary_record):
                    # HAProxy checks that every certificate file exists, so a new certificate has
                    # to be on the canary before its configuration can be validated there. The
                    # canary is recorded first: a failure from here on is reverted.
                    pushed.append((canary, plan))
                    await self._send_bundle(
                        canary_record,
                        plan.agents.get(canary_record.name),
                        plan.bundles.get(canary_record.name),
                    )
                    plan.bundle_sent.add(canary_record.name)
                await canary.validate(files)
                before: list[ServerStatus] = await canary.server_statuses()
        except (HaproxyError, AgentError) as exc:
            raise _RolloutFailedError(plan, f"SE group {group.name}: {exc}") from exc
        try:
            async with rec.stage("canary"):
                if canary_record.name not in plan.bundle_sent:
                    pushed.append((canary, plan))  # recorded first: a half-applied push is reverted
                await self._push_one(plan, canary_record, canary)
                await self._watch_canary(plan, canary_record, canary, before)
        except (HaproxyError, AgentError) as exc:
            raise _RolloutFailedError(plan, f"SE group {group.name}: {exc}") from exc
        async with rec.stage("rollout"):
            for record, engine in plan.engines[1:]:
                try:
                    pushed.append((engine, plan))
                    await self._push_one(plan, record, engine)
                    if await engine.fingerprint() != plan.target.fingerprint:
                        raise _RolloutFailedError(
                            plan,
                            f"SE group {group.name}: {record.name} is not running the "
                            "pushed config",
                        )
                except (HaproxyError, AgentError) as exc:
                    raise _RolloutFailedError(plan, f"SE group {group.name}: {exc}") from exc
        plan.notes["engines"] = [r.name for r, _ in plan.engines]
        if plan.bundles:
            plan.notes["bundles"] = {n: bundle_id(b) for n, b in sorted(plan.bundles.items())}

    async def _watch_canary(
        self,
        plan: _Plan,
        canary_record: EngineRecord,
        canary: HaproxyEngine,
        before: list[ServerStatus],
    ) -> None:
        group = plan.group
        plan.notes["canary"] = canary_record.name
        probe: RoutingProbe = self._probe
        agent = plan.agents.get(canary_record.name)
        if agent is not None and canary_record.name in plan.bundles:
            probe = AgentRoutingProbe(
                agent,
                expected_bundle=bundle_id(plan.bundles[canary_record.name]),
                expected_hashes=file_hashes(plan.bundles[canary_record.name]),
                sleep=self._sleep,
            )
        try:
            result = await canary_check(
                canary,
                group=group,
                expected_fingerprint=plan.target.fingerprint,
                before=before,
                probe=probe,
                window_s=float(group.canary_seconds),
                poll_s=self._poll,
                sleep=self._sleep,
                monotonic=time.monotonic,
            )
        except (HaproxyError, AgentError) as exc:
            raise _RolloutFailedError(plan, f"SE group {group.name}: canary check: {exc}") from exc
        plan.notes["canary_notes"] = list(result.notes)
        if not result.ok:
            raise _RolloutFailedError(
                plan, f"SE group {group.name}: canary failed: " + "; ".join(result.problems)
            )

    async def _revert(self, pushed: Sequence[tuple[HaproxyEngine, _Plan]]) -> dict[UUID, list[str]]:
        """Put every engine that took the new configuration back. Never raises."""
        notes: dict[UUID, list[str]] = {}
        for engine, plan in reversed(pushed):
            entry = notes.setdefault(plan.group.id, [])
            if plan.previous is None:
                entry.append(f"{engine.name}: no previous managed configuration to restore")
                continue
            try:
                await engine.push(plan.previous.files, previous=plan.target.files)
                if await engine.fingerprint() == plan.previous.fingerprint:
                    entry.append(f"{engine.name}: reverted")
                else:  # pragma: no cover - would indicate an engine bug
                    entry.append(f"{engine.name}: revert did not restore the previous fingerprint")
            except HaproxyError as exc:
                log.error("reverting %s failed: %s", engine.name, exc)
                entry.append(f"{engine.name}: REVERT FAILED: {exc}")
            agent = plan.agents.get(engine.name)
            before = plan.previous_bundles.get(engine.name)
            if agent is not None and before is not None:
                try:
                    result = await agent.apply_bundle(bundle_id(before), before)
                    entry.append(
                        f"{engine.name}: routing "
                        + ("reverted" if result.ok else f"REVERT FAILED: {result.error}")
                    )
                except AgentError as exc:
                    log.error("reverting routing on %s failed: %s", engine.name, exc)
                    entry.append(f"{engine.name}: routing REVERT FAILED: {exc}")
        return notes

    # -- outcomes --------------------------------------------------------------------------------

    async def _commit(
        self,
        tenant_id: UUID,
        change_set_id: UUID,
        base: Snapshot,
        draft: Draft,
        plans: list[_Plan],
        outcomes: list[GroupOutcome],
        request_id: str | None,
        started: datetime,
    ) -> ApplyReport:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            change_set = await store.get_change_set(session, change_set_id, for_update=True)
            if change_set is None or change_set.state is not ChangeSetState.APPLYING:
                raise PipelineStateError(f"change set {change_set_id} left the applying state")
            delta = await apply_delta(session, base, draft.snapshot)
            revision = None
            if not delta.is_empty:
                number = await store.current_revision(session, tenant_id) + 1
                revision = await store.insert_revision(
                    session,
                    tenant_id=tenant_id,
                    number=number,
                    change_set_id=change_set_id,
                    author=change_set.author,
                    summary=change_set.summary,
                    message=change_set.message,
                    snapshot=draft.snapshot.model_dump(mode="json"),
                )
            for outcome in outcomes:
                await store.insert_deployment(
                    session,
                    tenant_id=tenant_id,
                    change_set_id=change_set_id,
                    revision_id=revision.id if revision else None,
                    se_group_id=outcome.se_group_id,
                    state=outcome.state,
                    rendered_hash=outcome.rendered_hash,
                    fingerprint=outcome.fingerprint,
                    error=None,
                    details=outcome.details,
                    started_at=started,
                )
            await store.set_state(
                session, change_set_id, ChangeSetState.APPLIED, finished_at=func.now(), error=None
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=change_set.author,
                action="change_set.applied",
                object_ref=f"change_set:{change_set_id}",
                diff={
                    "revision": revision.number if revision else None,
                    "created": _count(delta.created),
                    "modified": _count(delta.modified),
                    "deleted": _count(delta.deleted),
                    "groups": [o.se_group_name for o in outcomes],
                },
                request_id=request_id,
            )
            if delta.deleted.get(ObjectType.NETWORK):
                # Deleted networks leave Proxmox SDN objects behind; the SDN job removes the ones
                # the controller created (ADR 0017).
                await queue.enqueue(
                    session,
                    "apply_sdn",
                    {"tenant_id": str(tenant_id)},
                    tenant_id=tenant_id,
                    max_attempts=1,
                    request_id=request_id,
                )
            if revision is not None:
                await queue.enqueue(
                    session,
                    "git_mirror",
                    {"tenant_id": str(tenant_id), "revision_number": revision.number},
                    tenant_id=tenant_id,
                    request_id=request_id,
                )
        return ApplyReport(
            ok=True,
            change_set_id=change_set_id,
            revision_number=revision.number if revision else None,
            groups=tuple(outcomes),
        )

    async def _fail(
        self,
        tenant_id: UUID,
        change_set_id: UUID,
        message: str,
        outcomes: list[GroupOutcome],
        request_id: str | None,
        started: datetime,
    ) -> ApplyReport:
        log.warning("change set %s failed: %s", change_set_id, message)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            change_set = await store.get_change_set(session, change_set_id, for_update=True)
            author = change_set.author if change_set else "system"
            for outcome in outcomes:
                await store.insert_deployment(
                    session,
                    tenant_id=tenant_id,
                    change_set_id=change_set_id,
                    revision_id=None,
                    se_group_id=outcome.se_group_id,
                    state=outcome.state,
                    rendered_hash=outcome.rendered_hash,
                    fingerprint=outcome.fingerprint,
                    error=outcome.error,
                    details=outcome.details,
                    started_at=started,
                )
            await store.set_state(
                session, change_set_id, ChangeSetState.FAILED, finished_at=func.now(), error=message
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=author,
                action="change_set.failed",
                object_ref=f"change_set:{change_set_id}",
                diff={"error": message, "groups": [o.se_group_name for o in outcomes]},
                request_id=request_id,
            )
        return ApplyReport(
            ok=False, change_set_id=change_set_id, error=message, groups=tuple(outcomes)
        )


class _PlanError(Exception):
    """A planning problem that fails the change set (for example a group with no engines)."""


def bundle_id(files: dict[str, str]) -> str:
    """Identify a routing bundle by the hash of its files (what the agent reports back)."""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(f"{path}\0{files[path]}\0".encode())
    return digest.hexdigest()[:24]


def file_hashes(files: dict[str, str]) -> dict[str, str]:
    """SHA-256 of each file, as the agent reports them."""
    return {p: hashlib.sha256(c.encode()).hexdigest() for p, c in files.items()}


def _renders_anything(result: RenderResult) -> bool:
    return "frontend " in result.files["haproxy/haproxy.cfg"]


def _outcome(
    plan: _Plan,
    state: DeploymentState,
    *,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> GroupOutcome:
    return GroupOutcome(
        se_group_id=plan.group.id,
        se_group_name=plan.group.name,
        state=state,
        rendered_hash=plan.target.content_hash,
        fingerprint=plan.target.fingerprint,
        error=error,
        details=details if details is not None else dict(plan.notes),
    )


def _count(group: dict[Any, list[UUID]]) -> int:
    return sum(len(ids) for ids in group.values())

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
from switchyard.db import audit_log
from switchyard.db import changesets as store
from switchyard.db.repository import load_snapshot
from switchyard.db.store import ConcurrentModificationError, apply_delta
from switchyard.db.tenancy import tenant_session
from switchyard.domain import models as dm
from switchyard.domain.changes import ChangeError, Draft, apply_ops
from switchyard.domain.errors import DomainValidationError
from switchyard.domain.models import SeGroup
from switchyard.domain.snapshot import Snapshot
from switchyard.domain.workflow import ChangeSetState, DeploymentState
from switchyard.jobs import queue
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
from switchyard.render.routing import EngineSlot, render_agent_bundle
from switchyard.services.context import Clock, Databases, utcnow

log = logging.getLogger("switchyard.pipeline")


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
        self, tenant_id: UUID, change_set_id: UUID, *, request_id: str | None = None
    ) -> ApplyReport:
        """Apply a change set that is in the ``applying`` state.

        Raises:
            PipelineStateError: If the change set does not exist or is not ``applying``.
        """
        started = self._clock()
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
                    draft = apply_ops(base, ops, tenant_id=tenant_id)
                    self._require_valid(draft)
                    plans = await self._plan(tenant_id, base, draft, opened)
                except (ChangeError, DomainValidationError, RenderError, _PlanError) as exc:
                    return await self._fail(
                        tenant_id, change_set_id, str(exc), [], request_id, started
                    )
                async with self._locks.hold(p.group.id for p in plans):
                    if await self._load_base(tenant_id) != base:
                        continue  # state moved while waiting for the locks; plan again
                    return await self._execute(
                        tenant_id, change_set_id, base, draft, plans, request_id, started
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
            target = render_haproxy(draft.snapshot, group.id)
            existed = any(g.id == group.id for g in base.se_groups)
            previous: RenderResult | None = render_haproxy(base, group.id) if existed else None
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

    async def _secrets_for(self, snapshot: Snapshot, group: SeGroup) -> dict[str, str]:
        """Resolve the BGP session passwords the group's profile refers to."""
        refs = {
            peer.md5_ref
            for profile in snapshot.routing_profiles
            if profile.id == group.routing_profile_id and isinstance(profile, dm.BgpProfile)
            for peer in profile.peers
            if peer.md5_ref
        }
        found: dict[str, str] = {}
        for ref in sorted(refs):
            if self._secrets is None:
                raise _PlanError("BGP passwords are configured but no secrets backend is available")
            try:
                found[ref] = await self._secrets.get(ref)
            except LookupError as exc:
                raise _PlanError(f"secret {ref} for BGP peer authentication not found") from exc
        return found

    async def _plan_bundles(
        self, plan: _Plan, base: Snapshot, after: Snapshot, records: list[EngineRecord]
    ) -> None:
        """Render each agent-managed engine's routing files, before and after the change."""
        group = plan.group
        if not plan.agents or group.routing_profile_id is None:
            return
        if not _renders_anything(plan.target):
            return
        secrets_after = await self._secrets_for(after, group)
        existed = plan.previous is not None
        secrets_before = await self._secrets_for(base, group) if existed else {}
        for index, record in enumerate(records):
            if record.name not in plan.agents:
                continue
            if group.ha_mode is dm.HaMode.BGP and not record.mgmt_ip:
                raise _PlanError(
                    f"engine {record.name} needs mgmt_ip: it is used as the BGP router id"
                )
            slot = EngineSlot(record.name, index, record.mgmt_ip or "0.0.0.0")  # noqa: S104
            plan.bundles[record.name] = render_agent_bundle(after, group.id, slot, secrets_after)
            if existed:
                with contextlib.suppress(RenderError):
                    plan.previous_bundles[record.name] = render_agent_bundle(
                        base, group.id, slot, secrets_before
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
    ) -> ApplyReport:
        pushed: list[tuple[HaproxyEngine, _Plan]] = []
        outcomes: list[GroupOutcome] = []
        try:
            for plan in plans:
                await self._rollout(plan, pushed)
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
        """Push HAProxy files, then the routing bundle, to one engine."""
        previous_files = plan.previous.files if plan.previous else None
        await engine.push(plan.target.files, previous=previous_files)
        agent = plan.agents.get(record.name)
        bundle = plan.bundles.get(record.name)
        if agent is not None and bundle is not None:
            result = await agent.apply_bundle(bundle_id(bundle), bundle)
            if not result.ok:
                note = " (previous files restored)" if result.restored_previous else ""
                raise HaproxyError(
                    f"{record.name}: agent rejected the routing bundle: {result.error}{note}"
                )

    async def _rollout(self, plan: _Plan, pushed: list[tuple[HaproxyEngine, _Plan]]) -> None:
        """Validate, push to the canary, watch it, then push to the rest."""
        group, files = plan.group, plan.target.files
        canary_record, canary = plan.engines[0]
        try:
            if self._sandbox is not None:
                await require_valid(self._sandbox, files)
            await canary.validate(files)
            before: list[ServerStatus] = await canary.server_statuses()
            pushed.append((canary, plan))  # recorded first: a half-applied push must be reverted
            await self._push_one(plan, canary_record, canary)
        except (HaproxyError, AgentError) as exc:
            raise _RolloutFailedError(plan, f"SE group {group.name}: {exc}") from exc
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
        for record, engine in plan.engines[1:]:
            try:
                pushed.append((engine, plan))
                await self._push_one(plan, record, engine)
                if await engine.fingerprint() != plan.target.fingerprint:
                    raise _RolloutFailedError(
                        plan,
                        f"SE group {group.name}: {record.name} is not running the pushed config",
                    )
            except (HaproxyError, AgentError) as exc:
                raise _RolloutFailedError(plan, f"SE group {group.name}: {exc}") from exc
        plan.notes["engines"] = [r.name for r, _ in plan.engines]
        if plan.bundles:
            plan.notes["bundles"] = {n: bundle_id(b) for n, b in sorted(plan.bundles.items())}

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

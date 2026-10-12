"""Alerting: per-tenant targets and rules, and the evaluator that tells people (ADR 0120 to 0122).

:class:`AlertService` is the tenant-facing CRUD (RLS applies; tenant administrators write, anyone
in the tenant reads). :class:`AlertEvaluator` is the scheduled job: every minute it looks at each
tenant's enabled rules, decides what changed, and sends one message per target. Three kinds of
rule exist:

- ``event``: an audit event with one of the named actions was written since the last look.
- ``metric``: a built-in series (see :data:`switchyard.domain.alerts.METRICS`) of a service or an
  engine is over or under a threshold for a number of minutes; a resolved message follows.
- ``cert_expiry``: a certificate has crossed one of the day thresholds; one message per tier, and
  a resolved message once it is renewed.

A failed delivery never stops the others and is recorded; alert state changes whether or not the
message got through, so a dead webhook cannot make the evaluator re-send forever.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select

from switchyard.adapters.notify import Notifier, NotifyError
from switchyard.adapters.secrets import SecretNotFoundError, SecretsReader
from switchyard.adapters.telemetry import Telemetry, TelemetryError
from switchyard.db import audit_log
from switchyard.db.alert_models import (
    AlertDeliveryRow,
    AlertRuleRow,
    AlertStateRow,
    AlertTargetRow,
)
from switchyard.db.audit_actions import CATALOG
from switchyard.db.models import CertificateRow, TenantRow, VirtualServiceRow
from switchyard.db.ops_models import ServiceEngineRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.alerts import (
    METRICS,
    Alert,
    AlertInvalid,
    metric_breached,
    validate_rule,
    validate_target,
)
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import AlreadyExistsError, FieldErrors, NotFoundError

log = logging.getLogger("switchyard.alerts")

EVALUATE_ALERTS_JOB = "evaluate_alerts"
#: Audit actions an event rule may name: everything tenant-visible, never sign-in or IdP events.
EVENT_ACTIONS = frozenset(a.action for a in CATALOG if not a.action.startswith(("auth.", "idp.")))
DELIVERY_KEEP = timedelta(days=30)
#: A runaway rule cannot send more than this many event messages per evaluation.
MAX_EVENTS_PER_RUN = 25
#: Metric rules look at this many engines per tenant at most.
MAX_ENGINES = 30


def _raise(exc: AlertInvalid) -> FieldErrors:
    errors = FieldErrors()
    for e in exc.errors:
        errors.add(e["path"], e["code"], e["message"])
    return errors


def _target_json(row: AlertTargetRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "config": row.config,
        "secret_ref": row.secret_ref,
        "enabled": row.enabled,
        "version": row.version,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


def _rule_json(row: AlertRuleRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "spec": row.spec,
        "targets": row.targets,
        "enabled": row.enabled,
        "version": row.version,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


class AlertService:
    """Targets and rules of one tenant, plus what is firing and what was sent."""

    def __init__(
        self,
        db: Databases,
        notifier: Notifier | None = None,
        secrets: SecretsReader | None = None,
        *,
        allow_http: bool = False,
        allow_private: bool = False,
        clock: Clock = utcnow,
    ) -> None:
        """Create the service.

        Args:
            db: Database handles.
            notifier: Sends the test message; ``None`` makes ``test`` answer unavailable.
            secrets: Resolves a target's ``secret_ref``.
            allow_http: Accept ``http://`` webhook URLs (labs).
            allow_private: Accept private addresses in webhook URLs (labs).
            clock: Time source.
        """
        self._db = db
        self._notifier = notifier
        self._secrets = secrets
        self._allow_http = allow_http
        self._allow_private = allow_private
        self._clock = clock

    # -- targets ---------------------------------------------------------------

    async def list_targets(self, principal: Principal, tenant_id: UUID) -> list[dict[str, Any]]:
        """All targets of the tenant (any role in the tenant)."""
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = (
                await session.execute(select(AlertTargetRow).order_by(AlertTargetRow.name))
            ).scalars()
            return [_target_json(r) for r in rows]

    async def create_target(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        name: str,
        kind: str,
        config: dict[str, Any],
        secret_ref: str | None = None,
        enabled: bool = True,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a target (tenant administrators).

        Raises:
            FieldErrors: Invalid ``name``, ``config`` or ``secret_ref``.
            AlreadyExistsError: The name is taken.
        """
        require(principal, "write", "infrastructure")
        clean = self._clean_target(name, kind, config, secret_ref)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            if (
                await session.execute(select(AlertTargetRow.id).where(AlertTargetRow.name == name))
            ).first():
                raise AlreadyExistsError(f"alert target {name!r} already exists")
            row = AlertTargetRow(
                id=uuid7(),
                tenant_id=tenant_id,
                name=name,
                kind=kind,
                config=clean,
                secret_ref=secret_ref,
                enabled=enabled,
                created_by=principal.actor,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="alert_target.create",
                object_ref=f"alert_target:{name}",
                diff={"kind": kind},
                request_id=request_id,
            )
            return _target_json(row)

    def _clean_target(
        self, name: str, kind: str, config: dict[str, Any], secret_ref: str | None
    ) -> dict[str, Any]:
        errors = FieldErrors()
        from switchyard.domain.validators import validate_name

        try:
            validate_name(name)
        except ValueError as exc:
            errors.add("name", "invalid", str(exc))
        if secret_ref is not None and (kind != "webhook" or not secret_ref.startswith("kv/")):
            errors.add("secret_ref", "invalid", "only webhooks sign, with a kv/... secret path")
        if errors:
            errors.raise_if_any()
        try:
            return validate_target(
                kind, config, allow_http=self._allow_http, allow_private=self._allow_private
            )
        except AlertInvalid as exc:
            raise _raise(exc) from exc

    async def update_target(
        self,
        principal: Principal,
        tenant_id: UUID,
        name: str,
        *,
        config: dict[str, Any] | None = None,
        secret_ref: str | None = None,
        enabled: bool | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Change a target's config, secret reference or enabled flag (tenant administrators)."""
        require(principal, "write", "infrastructure")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = (
                await session.execute(select(AlertTargetRow).where(AlertTargetRow.name == name))
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"alert target {name!r} not found")
            if config is not None or secret_ref is not None:
                row.config = self._clean_target(
                    name,
                    row.kind,
                    config if config is not None else row.config,
                    secret_ref if secret_ref is not None else row.secret_ref,
                )
            if secret_ref is not None:
                row.secret_ref = secret_ref
            if enabled is not None:
                row.enabled = enabled
            row.version += 1
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="alert_target.update",
                object_ref=f"alert_target:{name}",
                request_id=request_id,
            )
            await session.flush()
            return _target_json(row)

    async def delete_target(
        self, principal: Principal, tenant_id: UUID, name: str, request_id: str | None = None
    ) -> None:
        """Delete a target. Rules naming it simply skip it from then on."""
        require(principal, "write", "infrastructure")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            result = await session.execute(
                delete(AlertTargetRow).where(AlertTargetRow.name == name)
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise NotFoundError(f"alert target {name!r} not found")
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="alert_target.delete",
                object_ref=f"alert_target:{name}",
                request_id=request_id,
            )

    async def test_target(self, principal: Principal, tenant_id: UUID, name: str) -> dict[str, Any]:
        """Send a test message through the target now and report the outcome."""
        require(principal, "write", "infrastructure")
        if self._notifier is None:
            return {"ok": False, "error": "no notifier is configured"}
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = (
                await session.execute(select(AlertTargetRow).where(AlertTargetRow.name == name))
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"alert target {name!r} not found")
            tenant = await session.get(TenantRow, tenant_id)
            tenant_name = tenant.name if tenant else str(tenant_id)
            kind, config, ref = row.kind, dict(row.config), row.secret_ref
        now = self._clock()
        alert = Alert(
            tenant=tenant_name,
            rule=None,
            state="test",
            key=f"target:{name}",
            title="Test message from Switchyard",
            text=f"This is a test of the alert target {name!r}. Nothing is wrong.",
            at=now,
        )
        error: str | None = None
        try:
            await self._notifier.send(kind, config, await _secret(self._secrets, ref), alert)
        except NotifyError as exc:
            error = str(exc)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            session.add(
                AlertDeliveryRow(
                    id=uuid7(),
                    tenant_id=tenant_id,
                    rule=None,
                    target=name,
                    key=alert.key,
                    kind="test",
                    status="failed" if error else "sent",
                    error=error,
                    at=now,
                )
            )
        return {"ok": error is None, "error": error}

    # -- rules -----------------------------------------------------------------

    async def list_rules(self, principal: Principal, tenant_id: UUID) -> list[dict[str, Any]]:
        """All rules of the tenant (any role in the tenant)."""
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = (
                await session.execute(select(AlertRuleRow).order_by(AlertRuleRow.name))
            ).scalars()
            return [_rule_json(r) for r in rows]

    async def _targets(self, session: Any) -> set[str]:
        return set((await session.execute(select(AlertTargetRow.name))).scalars())

    async def create_rule(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        name: str,
        kind: str,
        spec: dict[str, Any],
        targets: list[str],
        enabled: bool = True,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a rule (tenant administrators)."""
        require(principal, "write", "infrastructure")
        from switchyard.domain.validators import validate_name

        async with tenant_session(self._db.app, [tenant_id]) as session:
            errors = FieldErrors()
            try:
                validate_name(name)
            except ValueError as exc:
                errors.add("name", "invalid", str(exc))
            try:
                clean, names = validate_rule(
                    kind, spec, targets, await self._targets(session), EVENT_ACTIONS
                )
            except AlertInvalid as exc:
                for e in exc.errors:
                    errors.add(e["path"], e["code"], e["message"])
                clean, names = {}, []
            errors.raise_if_any()
            if (
                await session.execute(select(AlertRuleRow.id).where(AlertRuleRow.name == name))
            ).first():
                raise AlreadyExistsError(f"alert rule {name!r} already exists")
            row = AlertRuleRow(
                id=uuid7(),
                tenant_id=tenant_id,
                name=name,
                kind=kind,
                spec=clean,
                targets=names,
                enabled=enabled,
                last_eval_at=self._clock(),
                created_by=principal.actor,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="alert_rule.create",
                object_ref=f"alert_rule:{name}",
                diff={"kind": kind},
                request_id=request_id,
            )
            return _rule_json(row)

    async def update_rule(
        self,
        principal: Principal,
        tenant_id: UUID,
        name: str,
        *,
        spec: dict[str, Any] | None = None,
        targets: list[str] | None = None,
        enabled: bool | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Change a rule's spec, targets or enabled flag (tenant administrators)."""
        require(principal, "write", "infrastructure")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = (
                await session.execute(select(AlertRuleRow).where(AlertRuleRow.name == name))
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"alert rule {name!r} not found")
            try:
                clean, names = validate_rule(
                    row.kind,
                    spec if spec is not None else row.spec,
                    targets if targets is not None else row.targets,
                    await self._targets(session),
                    EVENT_ACTIONS,
                )
            except AlertInvalid as exc:
                raise _raise(exc) from exc
            row.spec, row.targets = clean, names
            if enabled is not None:
                row.enabled = enabled
            row.version += 1
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="alert_rule.update",
                object_ref=f"alert_rule:{name}",
                request_id=request_id,
            )
            await session.flush()
            return _rule_json(row)

    async def delete_rule(
        self, principal: Principal, tenant_id: UUID, name: str, request_id: str | None = None
    ) -> None:
        """Delete a rule and its state (tenant administrators)."""
        require(principal, "write", "infrastructure")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            result = await session.execute(delete(AlertRuleRow).where(AlertRuleRow.name == name))
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise NotFoundError(f"alert rule {name!r} not found")
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="alert_rule.delete",
                object_ref=f"alert_rule:{name}",
                request_id=request_id,
            )

    # -- what is happening -----------------------------------------------------

    async def status(
        self, principal: Principal, tenant_id: UUID, *, limit: int = 50
    ) -> dict[str, Any]:
        """What is firing now and the most recent deliveries (any role in the tenant)."""
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rules = {r.id: r.name for r in (await session.execute(select(AlertRuleRow))).scalars()}
            states = (
                await session.execute(
                    select(AlertStateRow)
                    .where(AlertStateRow.state.in_(["firing", "pending"]))
                    .order_by(AlertStateRow.since.desc())
                )
            ).scalars()
            deliveries = (
                await session.execute(
                    select(AlertDeliveryRow).order_by(AlertDeliveryRow.at.desc()).limit(limit)
                )
            ).scalars()
            return {
                "firing": [
                    {
                        "rule": rules.get(s.rule_id, "?"),
                        "key": s.key,
                        "state": s.state,
                        "since": s.since,
                        "details": s.details,
                    }
                    for s in states
                ],
                "deliveries": [
                    {
                        "rule": d.rule,
                        "target": d.target,
                        "key": d.key,
                        "kind": d.kind,
                        "status": d.status,
                        "error": d.error,
                        "at": d.at,
                    }
                    for d in deliveries
                ],
            }


async def _secret(secrets: SecretsReader | None, ref: str | None) -> str | None:
    if not ref or secrets is None:
        return None
    try:
        return await secrets.get(ref)
    except SecretNotFoundError:
        return None


class AlertEvaluator:
    """The scheduled job: decide what changed and tell the targets."""

    def __init__(
        self,
        db: Databases,
        notifier: Notifier,
        telemetry: Telemetry | None = None,
        secrets: SecretsReader | None = None,
        *,
        clock: Clock = utcnow,
    ) -> None:
        """Create the evaluator (``telemetry`` ``None`` skips metric rules)."""
        self._db = db
        self._notifier = notifier
        self._telemetry = telemetry
        self._secrets = secrets
        self._clock = clock

    async def run(self) -> dict[str, int]:
        """Evaluate every tenant once; return how many messages were sent and failed."""
        out = {"tenants": 0, "sent": 0, "failed": 0}
        async with admin_session(self._db.admin) as session:
            tenants = [(t.id, t.name) for t in (await session.execute(select(TenantRow))).scalars()]
        for tenant_id, tenant_name in tenants:
            try:
                sent, failed = await self._tenant(tenant_id, tenant_name)
            except Exception:  # one tenant's trouble must not stop the others
                log.exception("alert evaluation failed for tenant %s", tenant_name)
                continue
            out["tenants"] += 1
            out["sent"] += sent
            out["failed"] += failed
        async with admin_session(self._db.admin) as session:
            await session.execute(
                delete(AlertDeliveryRow).where(AlertDeliveryRow.at < self._clock() - DELIVERY_KEEP)
            )
        return out

    async def _tenant(self, tenant_id: UUID, tenant_name: str) -> tuple[int, int]:
        now = self._clock()
        pending: list[tuple[AlertRuleRow, Alert]] = []
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rules = list(
                (await session.execute(select(AlertRuleRow).where(AlertRuleRow.enabled))).scalars()
            )
            if not rules:
                return 0, 0
            for rule in rules:
                if rule.kind == "event":
                    pending += await self._events(session, tenant_id, tenant_name, rule, now)
                elif rule.kind == "cert_expiry":
                    pending += await self._certs(session, tenant_id, tenant_name, rule, now)
                elif rule.kind == "metric" and self._telemetry is not None:
                    pending += await self._metrics(session, tenant_id, tenant_name, rule, now)
        return await self._deliver(tenant_id, pending, now)

    # -- event rules -----------------------------------------------------------

    async def _events(
        self, session: Any, tenant_id: UUID, tenant: str, rule: AlertRuleRow, now: datetime
    ) -> list[tuple[AlertRuleRow, Alert]]:
        from switchyard.db.ops_models import AuditEventRow

        since = rule.last_eval_at or rule.created_at
        rows = (
            await session.execute(
                select(AuditEventRow)
                .where(
                    AuditEventRow.tenant_id == tenant_id,
                    AuditEventRow.at > since,
                    AuditEventRow.action.in_(rule.spec["actions"]),
                )
                .order_by(AuditEventRow.at)
                .limit(MAX_EVENTS_PER_RUN)
            )
        ).scalars()
        descriptions = {a.action: a.description for a in CATALOG}
        out = []
        for e in rows:
            out.append(
                (
                    rule,
                    Alert(
                        tenant=tenant,
                        rule=rule.name,
                        state="firing",
                        key=f"event:{e.action}:{e.object_ref or ''}",
                        title=f"Event: {e.action}",
                        text=(
                            f"{descriptions.get(e.action, e.action)} "
                            f"({e.object_ref or 'no object'}, by {e.actor})."
                        ),
                        at=e.at,
                        details={"action": e.action, "object": e.object_ref, "actor": e.actor},
                    ),
                )
            )
        rule.last_eval_at = now
        return out

    # -- state machine shared by certificate and metric rules ------------------

    async def _state(self, session: Any, rule: AlertRuleRow, key: str) -> AlertStateRow | None:
        found: AlertStateRow | None = (
            await session.execute(
                select(AlertStateRow).where(
                    AlertStateRow.rule_id == rule.id, AlertStateRow.key == key
                )
            )
        ).scalar_one_or_none()
        return found

    # -- certificate expiry ----------------------------------------------------

    async def _certs(
        self, session: Any, tenant_id: UUID, tenant: str, rule: AlertRuleRow, now: datetime
    ) -> list[tuple[AlertRuleRow, Alert]]:
        out: list[tuple[AlertRuleRow, Alert]] = []
        days: list[int] = sorted(rule.spec["days"])
        certs = (
            await session.execute(select(CertificateRow).where(CertificateRow.purpose == "server"))
        ).scalars()
        for cert in certs:
            left = (cert.not_after - now).total_seconds() / 86400
            # expired is its own tier (0), so "has expired" is announced after the day tiers
            tier = 0 if left <= 0 else next((d for d in days if left <= d), None)
            key = f"cert:{cert.name}"
            state = await self._state(session, rule, key)
            if tier is None:
                if state is not None and state.state == "firing":
                    state.state, state.since, state.details = "ok", now, {}
                    out.append(
                        (
                            rule,
                            Alert(
                                tenant,
                                rule.name,
                                "resolved",
                                key,
                                f"Certificate {cert.name} is valid again",
                                f"{cert.name} now expires on {cert.not_after:%Y-%m-%d}.",
                                now,
                                {"not_after": cert.not_after.isoformat()},
                            ),
                        )
                    )
                continue
            if state is not None and state.state == "firing" and state.details.get("tier") == tier:
                continue
            if state is None:
                state = AlertStateRow(
                    id=uuid7(), tenant_id=tenant_id, rule_id=rule.id, key=key, state="ok"
                )
                session.add(state)
            state.state, state.since, state.notified_at = "firing", now, now
            state.details = {"tier": tier, "not_after": cert.not_after.isoformat()}
            when = "has expired" if left <= 0 else f"expires in {max(0, int(left))} day(s)"
            out.append(
                (
                    rule,
                    Alert(
                        tenant,
                        rule.name,
                        "firing",
                        key,
                        f"Certificate {cert.name} {when}",
                        f"{cert.name} ({', '.join(cert.sans[:3])}) {when}: not valid after "
                        f"{cert.not_after:%Y-%m-%d %H:%M} UTC. Renew or replace it.",
                        now,
                        {"days_left": round(left, 1), "tier": tier, "sans": list(cert.sans)},
                    ),
                )
            )
        return out

    # -- metric rules ----------------------------------------------------------

    async def _values(
        self, session: Any, tenant_id: UUID, metric: str, scope: str | None
    ) -> dict[str, float | None]:
        telemetry = self._telemetry
        if telemetry is None:
            return {}
        kind = METRICS[metric][2]
        out: dict[str, float | None] = {}
        try:
            if kind == "vs":
                names = [
                    n
                    for n in (
                        await session.execute(
                            select(VirtualServiceRow.name).order_by(VirtualServiceRow.name)
                        )
                    ).scalars()
                    if scope in (None, n)
                ]
                summaries = await telemetry.summaries(tenant_id, names, window_s=300)
                for name, s in summaries.items():
                    value = {
                        "vs_5xx_ratio": None
                        if s.error_5xx_ratio is None
                        else s.error_5xx_ratio * 100,
                        "vs_p95_ms": s.p95_ms,
                        "vs_rps": s.rps,
                    }[metric]
                    out[f"vs:{name}"] = value
            else:
                names = [
                    n
                    for n in (
                        await session.execute(
                            select(ServiceEngineRow.name)
                            .order_by(ServiceEngineRow.name)
                            .limit(MAX_ENGINES)
                        )
                    ).scalars()
                    if scope in (None, n)
                ]
                for name in names:
                    m = (
                        await telemetry.engine_metrics(tenant_id, name, window_s=300, step_s=30)
                    ).summary
                    if metric == "engine_cpu_percent":
                        value = m.cpu_percent
                    elif metric == "engine_sessions":
                        value = m.haproxy_sessions
                    else:
                        value = (
                            100 * m.memory_used_bytes / m.memory_total_bytes
                            if m.memory_used_bytes is not None and m.memory_total_bytes
                            else None
                        )
                    out[f"engine:{name}"] = value
        except (TelemetryError, ValueError) as exc:
            log.warning("metric rule skipped: %s", exc)
            return {}
        return out

    async def _metrics(
        self, session: Any, tenant_id: UUID, tenant: str, rule: AlertRuleRow, now: datetime
    ) -> list[tuple[AlertRuleRow, Alert]]:
        spec = rule.spec
        label, unit, _ = METRICS[spec["metric"]]
        out: list[tuple[AlertRuleRow, Alert]] = []
        wait = timedelta(minutes=spec["for_minutes"])
        for key, value in (
            await self._values(session, tenant_id, spec["metric"], spec.get("scope"))
        ).items():
            if value is None:  # no data is neither a breach nor a recovery
                continue
            state = await self._state(session, rule, key)
            breached = metric_breached(spec["op"], value, spec["threshold"])
            what = key.split(":", 1)[1]
            facts = {"value": round(value, 2), "threshold": spec["threshold"], "unit": unit}
            sense = "above" if spec["op"] == "gt" else "below"
            if breached:
                if state is None:
                    state = AlertStateRow(
                        id=uuid7(), tenant_id=tenant_id, rule_id=rule.id, key=key, state="ok"
                    )
                    session.add(state)
                if state.state == "ok":
                    state.state, state.since = "pending", now
                if state.state == "pending" and now - state.since >= wait:
                    state.state, state.notified_at = "firing", now
                    state.details = facts
                    out.append(
                        (
                            rule,
                            Alert(
                                tenant,
                                rule.name,
                                "firing",
                                key,
                                f"{label} of {what} is {sense} {spec['threshold']} {unit}",
                                f"{label} of {what} is {round(value, 2)} {unit}, {sense} the "
                                f"threshold of {spec['threshold']} {unit} for "
                                f"{spec['for_minutes']} min.",
                                now,
                                facts,
                            ),
                        )
                    )
            elif state is not None and state.state in ("pending", "firing"):
                was_firing = state.state == "firing"
                state.state, state.since, state.details = "ok", now, {}
                if was_firing:
                    out.append(
                        (
                            rule,
                            Alert(
                                tenant,
                                rule.name,
                                "resolved",
                                key,
                                f"{label} of {what} is back to normal",
                                f"{label} of {what} is {round(value, 2)} {unit}.",
                                now,
                                facts,
                            ),
                        )
                    )
        return out

    # -- delivery --------------------------------------------------------------

    async def _deliver(
        self, tenant_id: UUID, pending: Sequence[tuple[AlertRuleRow, Alert]], now: datetime
    ) -> tuple[int, int]:
        if not pending:
            return 0, 0
        async with tenant_session(self._db.app, [tenant_id]) as session:
            targets = {
                t.name: (t.kind, dict(t.config), t.secret_ref)
                for t in (
                    await session.execute(select(AlertTargetRow).where(AlertTargetRow.enabled))
                ).scalars()
            }
        sent = failed = 0
        records: list[AlertDeliveryRow] = []
        for rule, alert in pending:
            for name in rule.targets:
                if name not in targets:
                    continue
                kind, config, ref = targets[name]
                error: str | None = None
                try:
                    await self._notifier.send(
                        kind, config, await _secret(self._secrets, ref), alert
                    )
                    sent += 1
                except NotifyError as exc:
                    error = str(exc)
                    failed += 1
                records.append(
                    AlertDeliveryRow(
                        id=uuid7(),
                        tenant_id=tenant_id,
                        rule=rule.name,
                        target=name,
                        key=alert.key,
                        kind=alert.state,
                        status="failed" if error else "sent",
                        error=error,
                        at=now,
                    )
                )
        async with tenant_session(self._db.app, [tenant_id]) as session:
            session.add_all(records)
        return sent, failed


__all__ = ["EVALUATE_ALERTS_JOB", "UTC", "AlertEvaluator", "AlertService"]

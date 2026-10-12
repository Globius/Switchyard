"""Alert targets, rules and the evaluator over a real database (ADR 0120 to 0123)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from switchyard.adapters.notify import FakeNotifier
from switchyard.adapters.telemetry import FakeTelemetry
from switchyard.api.app import build_services, create_app
from switchyard.db import audit_log
from switchyard.db.tenancy import admin_session
from switchyard.domain.telemetry import AccessLogEntry, MetricSummary
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.alerts import AlertEvaluator, AlertService
from switchyard.services.context import Principal
from tests.pipeline_env import TENANT, World, world

pytestmark = pytest.mark.usefixtures("owner_engine")

ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
V1 = "/api/v1/tenants/acme"
HOOK = "https://hooks.example.test/in"
BAD = "https://bad.example.test/in"


class Summaries(FakeTelemetry):
    """Telemetry whose headline numbers the test sets."""

    def __init__(self) -> None:
        super().__init__()
        self.p95: float | None = 5.0

    async def summaries(
        self, tenant_id: Any, services: list[str], *, window_s: int
    ) -> dict[str, MetricSummary]:
        self.asked.append(tenant_id)
        return {s: MetricSummary(rps=2.0, p95_ms=self.p95, error_5xx_ratio=0.0) for s in services}


class Env:
    def __init__(self, w: World, client: httpx.AsyncClient, notifier: FakeNotifier) -> None:
        self.w, self.client, self.notifier = w, client, notifier
        self.clock = [datetime.now(UTC)]
        self.telemetry = Summaries()
        self.evaluator = AlertEvaluator(w.db, notifier, self.telemetry, clock=lambda: self.clock[0])
        self.tokens: dict[str, dict[str, str]] = {}

    def h(self, who: str = "admin") -> dict[str, str]:
        return self.tokens[who]

    async def target(self, name: str = "ops", url: str = HOOK, **extra: Any) -> httpx.Response:
        body = {"name": name, "kind": "webhook", "config": {"url": url}} | extra
        return await self.client.post(f"{V1}/alert-targets", json=body, headers=self.h())

    async def rule(
        self, name: str, kind: str, spec: dict[str, Any], *targets: str
    ) -> httpx.Response:
        body = {"name": name, "kind": kind, "spec": spec, "targets": list(targets or ["ops"])}
        return await self.client.post(f"{V1}/alert-rules", json=body, headers=self.h())

    def titles(self) -> list[tuple[str, str]]:
        return [(a.state, a.title) for _, _, _, a in self.notifier.sent]


@pytest_asyncio.fixture
async def env(pg_url: str) -> AsyncIterator[Env]:
    async with world(pg_url) as w:
        notifier = FakeNotifier(fail={BAD})
        e = Env(w, httpx.AsyncClient(), notifier)
        alerts = AlertService(w.db, notifier, clock=lambda: e.clock[0])
        services = build_services(
            w.db, StaticEngineDirectory({}), alerts=alerts, telemetry=e.telemetry
        )
        other = (await services.tenants.create(ROOT, "globex")).id
        for who, role in (("admin", Role.TENANT_ADMIN), ("viewer", Role.VIEWER)):
            token = (await services.tokens.create(ROOT, TENANT, who, role))[0]
            e.tokens[who] = {"Authorization": f"Bearer {token}"}
        token = (await services.tokens.create(ROOT, other, "g", Role.TENANT_ADMIN))[0]
        e.tokens["globex"] = {"Authorization": f"Bearer {token}"}
        app = create_app(services)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as client:
            e.client = client
            yield e


async def test_targets_are_created_by_administrators_and_read_by_anyone(env: Env) -> None:
    made = await env.target()
    assert made.status_code == 201 and made.json()["config"] == {"url": HOOK}
    assert (await env.target()).status_code == 409
    denied = await env.client.post(
        f"{V1}/alert-targets",
        json={"name": "x", "kind": "webhook", "config": {"url": HOOK}},
        headers=env.h("viewer"),
    )
    assert denied.status_code == 403 and denied.json()["required_role"] == "tenant-admin"
    listed = await env.client.get(f"{V1}/alert-targets", headers=env.h("viewer"))
    assert [t["name"] for t in listed.json()] == ["ops"]
    off = await env.client.patch(
        f"{V1}/alert-targets/ops", json={"enabled": False}, headers=env.h()
    )
    assert off.json()["enabled"] is False and off.json()["version"] == 2
    assert (await env.client.delete(f"{V1}/alert-targets/ops", headers=env.h())).status_code == 204
    assert (await env.client.delete(f"{V1}/alert-targets/ops", headers=env.h())).status_code == 404


async def test_dangerous_targets_are_refused_with_field_paths(env: Env) -> None:
    for url, code in (
        ("https://169.254.169.254/latest", "private_address"),
        ("http://hooks.example.test/in", "scheme"),
        ("https://u:p@hooks.example.test/in", "credentials"),
    ):
        r = await env.target(url=url)
        assert r.status_code == 422, url
        assert r.json()["errors"][0] == {
            "path": "config.url",
            "code": code,
            "message": r.json()["errors"][0]["message"],
        }
    assert (await env.target(secret_ref="plaintext-secret")).status_code == 422
    assert (await env.target(name="Bad Name")).status_code == 422
    # the secret reference is stored, never a secret
    ok = await env.target(name="signed", secret_ref="kv/tenants/acme/alerts/hook")
    assert ok.status_code == 201 and ok.json()["secret_ref"] == "kv/tenants/acme/alerts/hook"


async def test_a_tenant_never_sees_another_tenants_alerting(env: Env) -> None:
    await env.target()
    await env.rule("r", "cert_expiry", {})
    assert (
        await env.client.get("/api/v1/tenants/globex/alert-targets", headers=env.h("globex"))
    ).json() == []
    assert (
        await env.client.get("/api/v1/tenants/globex/alert-rules", headers=env.h("globex"))
    ).json() == []
    assert (await env.client.get(f"{V1}/alert-targets", headers=env.h("globex"))).status_code == 404
    async with admin_session(env.w.db.admin) as s:
        assert (await s.execute(text("SELECT count(*) FROM alert_target"))).scalar_one() == 1


async def test_rules_are_validated_against_the_tenants_targets(env: Env) -> None:
    await env.target()
    assert (await env.rule("expiry", "cert_expiry", {"days": [30, 7]})).status_code == 201
    bad_target = await env.rule("x", "cert_expiry", {}, "nobody")
    assert bad_target.json()["errors"][0]["path"] == "targets[0]"
    promql = await env.rule("y", "metric", {"metric": "up or vector(1)", "threshold": 1})
    assert promql.status_code == 422
    auth = await env.rule("z", "event", {"actions": ["auth.login"]})
    assert auth.json()["errors"][0]["code"] == "unknown_action"
    patched = await env.client.patch(
        f"{V1}/alert-rules/expiry", json={"spec": {"days": [90]}, "enabled": False}, headers=env.h()
    )
    assert patched.json()["spec"] == {"days": [90]} and patched.json()["enabled"] is False
    cat = (await env.client.get("/api/v1/alert-catalog", headers=env.h("viewer"))).json()
    assert (
        "vs_5xx_ratio" in {m["name"] for m in cat["metrics"]}
        and "change_set.failed" in cat["event_actions"]
    )
    assert not any(a.startswith("auth.") for a in cat["event_actions"])


async def test_the_test_message_reports_success_and_failure_and_is_logged(env: Env) -> None:
    await env.target()
    await env.target(name="broken", url=BAD)
    ok = await env.client.post(f"{V1}/alert-targets/ops/test", headers=env.h())
    assert ok.json() == {"ok": True, "error": None}
    assert env.notifier.sent[0][3].state == "test"
    bad = await env.client.post(f"{V1}/alert-targets/broken/test", headers=env.h())
    assert bad.json()["ok"] is False and "HTTP 500" in bad.json()["error"]
    status = (await env.client.get(f"{V1}/alerts", headers=env.h("viewer"))).json()
    assert sorted((d["target"], d["status"], d["kind"]) for d in status["deliveries"]) == [
        ("broken", "failed", "test"),
        ("ops", "sent", "test"),
    ]


async def cert_not_after(env: Env) -> datetime:
    async with admin_session(env.w.db.admin) as s:
        row = (
            await s.execute(text("SELECT name, not_after FROM certificate ORDER BY name LIMIT 1"))
        ).one()
    return row[1]  # type: ignore[no-any-return]


async def test_certificate_expiry_notifies_once_per_tier_and_when_renewed(env: Env) -> None:
    await env.target()
    await env.target(name="broken", url=BAD)
    await env.rule("expiry", "cert_expiry", {"days": [30, 14, 7]}, "broken", "ops")
    not_after = await cert_not_after(env)
    env.clock[0] = not_after - timedelta(days=60)
    assert (await env.evaluator.run())["sent"] == 0
    env.clock[0] = not_after - timedelta(days=20)  # crossed 30
    first = await env.evaluator.run()
    assert first == {
        "tenants": 2,
        "sent": 1,
        "failed": 1,
    }  # the broken target never blocks the good one
    assert env.titles()[0][0] == "firing" and "expires in 20 day" in env.titles()[0][1]
    assert (await env.evaluator.run())["sent"] == 0  # same tier: not again
    env.clock[0] = not_after - timedelta(days=10)  # crossed 14
    assert (await env.evaluator.run())["sent"] == 1
    env.clock[0] = not_after - timedelta(days=5)  # crossed 7
    assert (await env.evaluator.run())["sent"] == 1
    env.clock[0] = not_after + timedelta(days=1)
    assert (await env.evaluator.run())["sent"] == 1 and "has expired" in env.titles()[-1][1]
    # renewing it resolves the alert, once
    async with admin_session(env.w.db.admin) as s:
        await s.execute(
            text("UPDATE certificate SET not_after = :n"), {"n": not_after + timedelta(days=365)}
        )
    before = len(env.notifier.sent)
    assert (await env.evaluator.run())["sent"] == 1
    assert env.titles()[-1][0] == "resolved" and len(env.notifier.sent) == before + 1
    assert (await env.evaluator.run())["sent"] == 0
    status = (await env.client.get(f"{V1}/alerts", headers=env.h())).json()
    assert status["firing"] == []
    assert any(d["status"] == "failed" and d["target"] == "broken" for d in status["deliveries"])


async def test_event_rules_tell_about_audit_events_after_they_were_created(env: Env) -> None:
    await env.target()
    async with admin_session(env.w.db.admin) as s:  # before the rule: never reported
        await audit_log.record(
            s,
            tenant_id=TENANT,
            actor="user:a",
            action="change_set.failed",
            object_ref="change_set:old",
        )
    env.clock[0] = datetime.now(UTC)
    await env.rule("failures", "event", {"actions": ["change_set.failed"]})
    async with admin_session(env.w.db.admin) as s:
        await audit_log.record(
            s,
            tenant_id=TENANT,
            actor="user:a",
            action="change_set.failed",
            object_ref="change_set:new",
        )
        await audit_log.record(
            s, tenant_id=TENANT, actor="user:a", action="change_set.open", object_ref="change_set:x"
        )
    env.clock[0] = datetime.now(UTC) + timedelta(seconds=5)
    assert (await env.evaluator.run())["sent"] == 1
    alert = env.notifier.sent[0][3]
    assert (
        alert.key == "event:change_set.failed:change_set:new" and alert.details["actor"] == "user:a"
    )
    env.clock[0] += timedelta(minutes=1)
    assert (await env.evaluator.run())["sent"] == 0  # already reported


async def test_metric_rules_wait_out_their_duration_and_report_recovery(env: Env) -> None:
    await env.target()
    await env.rule(
        "slow", "metric", {"metric": "vs_p95_ms", "op": "gt", "threshold": 100, "for_minutes": 5}
    )
    env.telemetry.p95 = 250.0
    assert (await env.evaluator.run())["sent"] == 0  # pending
    status = (await env.client.get(f"{V1}/alerts", headers=env.h())).json()
    assert status["firing"][0]["state"] == "pending"
    waiting = status["firing"][0]
    assert waiting["details"]["value"] == 250.0 and waiting["details"]["threshold"] == 100
    assert waiting["fires_at"] is not None
    assert datetime.fromisoformat(waiting["fires_at"]) - datetime.fromisoformat(
        waiting["since"]
    ) == timedelta(minutes=5)
    env.clock[0] += timedelta(minutes=3)
    assert (await env.evaluator.run())["sent"] == 0
    env.clock[0] += timedelta(minutes=3)
    assert (await env.evaluator.run())["sent"] == 1
    assert "p95" in env.titles()[0][1].lower() or "response time" in env.titles()[0][1].lower()
    assert env.notifier.sent[0][3].details["value"] == 250.0
    env.clock[0] += timedelta(minutes=1)
    assert (await env.evaluator.run())["sent"] == 0  # still firing: no repeat
    env.telemetry.p95 = None  # no data is neither breach nor recovery
    assert (await env.evaluator.run())["sent"] == 0
    env.telemetry.p95 = 20.0
    assert (await env.evaluator.run())["sent"] == 1
    assert env.titles()[-1][0] == "resolved"
    # a blip shorter than the duration never fires and never reports
    env.telemetry.p95 = 250.0
    await env.evaluator.run()
    env.telemetry.p95 = 20.0
    assert (await env.evaluator.run())["sent"] == 0


async def test_disabled_rules_and_targets_stay_quiet_and_deleting_cleans_up(env: Env) -> None:
    await env.target()
    await env.rule("expiry", "cert_expiry", {"days": [3650]})
    await env.client.patch(f"{V1}/alert-rules/expiry", json={"enabled": False}, headers=env.h())
    assert (await env.evaluator.run())["sent"] == 0
    await env.client.patch(f"{V1}/alert-rules/expiry", json={"enabled": True}, headers=env.h())
    await env.client.patch(f"{V1}/alert-targets/ops", json={"enabled": False}, headers=env.h())
    assert (await env.evaluator.run())["sent"] == 0 and env.notifier.sent == []
    await env.client.patch(f"{V1}/alert-targets/ops", json={"enabled": True}, headers=env.h())
    await env.client.delete(f"{V1}/alert-rules/expiry", headers=env.h())
    async with admin_session(env.w.db.admin) as s:
        assert (await s.execute(text("SELECT count(*) FROM alert_state"))).scalar_one() == 0


async def test_the_tenant_wide_log_view_filters_by_service(env: Env) -> None:
    now = datetime.now(UTC)

    def entry(frontend: str, status: int) -> AccessLogEntry:
        return AccessLogEntry(
            at=now - timedelta(minutes=1),
            client_ip="192.0.2.1",
            frontend=frontend,
            backend="p",
            server="s",
            status=status,
            bytes=1,
            duration_ms=1,
            raw=frontend,
        )

    env.telemetry.entries = [entry("vs_web", 200), entry("vs_api", 502)]
    allv = (await env.client.get(f"{V1}/logs", headers=env.h("viewer"))).json()
    assert allv["available"] and {e["frontend"] for e in allv["entries"]} == {"vs_web", "vs_api"}
    one = (await env.client.get(f"{V1}/logs?service=api", headers=env.h("viewer"))).json()
    assert [e["frontend"] for e in one["entries"]] == ["vs_api"]
    errs = (await env.client.get(f"{V1}/logs?status_class=5", headers=env.h("viewer"))).json()
    assert [e["status"] for e in errs["entries"]] == [502]
    unsafe = (
        await env.client.get(f"{V1}/logs", params={"service": 'x"}'}, headers=env.h("viewer"))
    ).json()
    assert unsafe["available"] is False
    assert set(env.telemetry.asked) == {TENANT}

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Changing the controller's FQDN, VIP and certificate with a way back (ADR 0081)."""

import copy
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.controller_settings import ControllerSettings
from switchyard.services.errors import ConflictError, FieldErrors, PermissionDeniedError
from tests.factories import base_data
from tests.pipeline_env import GROUP, TENANT, World, engine_record, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")
V1 = "/api/v1"
ROOT = principal(Role.SYSTEM_ADMIN, "root")
OLD, NEW = "switchyard.example.test", "sy.example.test"


def _d(value: dict[str, Any] | None) -> dict[str, Any]:
    """The value, which the test expects to be set."""
    assert value is not None
    return value


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def management_data(*, with_tls: bool = True) -> dict[str, Any]:
    """The base data, turned into the locked management tenant with a ``controller`` service."""
    data = copy.deepcopy(base_data())
    data["tenants"][0]["name"] = "management"
    data["se_groups"][0]["canary_seconds"] = 0
    vs = data["virtual_services"][0]
    vs["name"] = "controller"
    if not with_tls:
        vs["ports"] = [{"port": 8000}]
        vs.pop("certificate_ids", None)
    vs.pop("routes", None)
    vs["default_pool_id"] = "pool-web"
    cert = data["certificates"][0]
    cert["name"] = "controller"
    cert["sans"] = [OLD, NEW]
    cert["secret_ref"] = "kv/tenants/management/certs/controller"
    data["certificates"].append(
        {
            **cert,
            "id": "cert-new",
            "name": "controller-new",
            "sans": [NEW, f"*.{NEW}"],
            "secret_ref": "kv/tenants/management/certs/controller-new",
        }
    )
    data["certificates"].append(
        {
            **cert,
            "id": "cert-short",
            "name": "controller-short",
            "sans": [NEW],
            "not_after": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
        }
    )
    data["certificates"].append(
        {**cert, "id": "cert-other", "name": "controller-other", "sans": ["other.example.test"]}
    )
    return data


@dataclass
class Rig:
    w: World
    controller: ControllerSettings
    clock: Clock
    services: ApiServices

    async def run_change_set(self) -> None:
        async with admin_session(self.w.db.admin) as s:
            cs = (
                await s.execute(text("SELECT id FROM change_set ORDER BY created_at DESC LIMIT 1"))
            ).scalar_one()
        report = await self.w.pipeline.run(TENANT, UUID(str(cs)))
        assert report.ok, report.error

    async def vs(self) -> Any:
        snap = await self.w.snapshot()
        return next(s for s in snap.virtual_services if s.name == "controller")

    async def sql(self, query: str) -> Any:
        async with admin_session(self.w.db.admin) as s:
            return (await s.execute(text(query))).scalar_one()


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url, management_data()) as w:
        clock = Clock()
        services = build_services(
            w.db,
            StaticEngineDirectory({GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}),
            MemorySecrets(),
        )
        yield Rig(w, ControllerSettings(w.db, w.service, clock=clock), clock, services)


async def test_a_change_applies_waits_and_is_confirmed_from_the_new_address(rig: Rig) -> None:
    before = await rig.vs()
    view = await rig.controller.propose(
        ROOT, fqdn=NEW, vip="192.0.2.31", certificate="controller-new", confirm_within_s=120
    )
    assert view.managed and _d(view.pending)["state"] == "applying"
    assert _d(view.pending)["after"]["vip"] == "192.0.2.31"
    assert str((await rig.vs()).vip) == str(before.vip), "nothing changed until the change set ran"
    await rig.run_change_set()
    assert str((await rig.vs()).vip) == "192.0.2.31"
    reason = await rig.sql(
        "SELECT break_glass_reason FROM change_set ORDER BY created_at DESC LIMIT 1"
    )
    assert "controller settings" in reason, "the management tenant needs a recorded reason"

    assert await rig.controller.tick() == ["waiting"]
    view = await rig.controller.view(ROOT)
    assert _d(view.pending)["state"] == "waiting" and _d(view.pending)["seconds_left"] == 120
    assert _d(view.pending)["revision_after"] == _d(view.pending)["revision_before"] + 1

    with pytest.raises(PermissionDeniedError, match=NEW):
        await rig.controller.confirm(ROOT, request_host=OLD)
    done = await rig.controller.confirm(ROOT, request_host=f"{NEW}:443")
    assert done.pending is None and done.public_url == f"https://{NEW}"
    assert _d(done.last_result)["kind"] == "confirmed"
    assert str((await rig.vs()).vip) == "192.0.2.31", "confirming keeps what was applied"
    assert await rig.controller.tick() == [], "nothing is left to watch"
    assert await rig.sql("SELECT count(*) FROM audit_event WHERE action LIKE 'controller.%'") == 2


async def test_an_unconfirmed_change_is_reverted_by_the_worker_and_keeps_other_edits(
    rig: Rig,
) -> None:
    original = await rig.vs()
    await rig.controller.propose(ROOT, fqdn=NEW, vip="192.0.2.32", certificate="controller-new")
    await rig.run_change_set()
    await rig.controller.tick()

    rig.clock.advance(119)
    assert await rig.controller.tick() == [], "the window is still open"
    rig.clock.advance(2)
    assert await rig.controller.tick() == ["expired"]
    await rig.run_change_set()  # the worker applies the revert like any change set
    restored = await rig.vs()
    assert str(restored.vip) == str(original.vip)
    assert restored.certificate_ids == original.certificate_ids
    view = await rig.controller.view(ROOT)
    assert view.pending is None and view.public_url is None
    assert _d(view.last_result)["kind"] == "reverted" and _d(view.last_result)["restored"] is True
    assert "not confirmed" in _d(view.last_result)["reason"]


async def test_revert_now_and_the_command_line_do_the_same(rig: Rig) -> None:
    original = await rig.vs()
    await rig.controller.propose(ROOT, fqdn=NEW, vip="192.0.2.33")
    await rig.run_change_set()
    await rig.controller.tick()
    view = await rig.controller.revert(ROOT)
    assert view.pending is None and _d(view.last_result)["by"] == ROOT.actor
    await rig.run_change_set()
    assert str((await rig.vs()).vip) == str(original.vip)
    with pytest.raises(ConflictError):
        await rig.controller.revert(ROOT)

    await rig.controller.propose(ROOT, fqdn=NEW, vip="192.0.2.34")
    await rig.run_change_set()
    await rig.controller.tick()
    assert await rig.controller.reset() is True, "recovery needs only the database"
    await rig.run_change_set()
    assert str((await rig.vs()).vip) == str(original.vip)
    assert await rig.controller.reset() is False
    assert "last result: reverted" in await rig.controller.status_text()


async def test_a_change_that_fails_to_apply_clears_itself(rig: Rig) -> None:
    await rig.controller.propose(ROOT, fqdn=NEW, vip="192.0.2.35")
    rig.w.engines["se-a"].fail_next("rejected")  # the canary refuses the new configuration
    async with admin_session(rig.w.db.admin) as s:
        cs = (
            await s.execute(text("SELECT id FROM change_set ORDER BY created_at DESC LIMIT 1"))
        ).scalar_one()
    report = await rig.w.pipeline.run(TENANT, UUID(str(cs)))
    assert not report.ok
    assert await rig.controller.tick() == ["failed"]
    view = await rig.controller.view(ROOT)
    assert view.pending is None and _d(view.last_result)["kind"] == "failed"
    assert str((await rig.vs()).vip) != "192.0.2.35"


async def test_bad_proposals_are_refused_with_the_field(rig: Rig) -> None:
    async def problems(**kwargs: Any) -> set[str]:
        with pytest.raises(FieldErrors) as caught:
            await rig.controller.propose(ROOT, **kwargs)
        return {e["path"] for e in caught.value.errors}

    assert await problems(fqdn="not a host") == {"fqdn"}
    assert await problems(fqdn=NEW, vip="nope") == {"vip"}
    assert await problems(fqdn=NEW, certificate="ghost") == {"certificate"}
    assert await problems(fqdn=NEW, certificate="controller-other") == {"fqdn"}, "does not cover"
    assert await problems(fqdn=NEW, certificate="controller-short") == {"certificate"}
    assert await problems(fqdn=NEW, vip="203.0.113.5") == {"vip"}, "outside the VIP network"
    assert (await rig.controller.view(ROOT)).pending is None
    assert await rig.sql("SELECT count(*) FROM change_set WHERE state = 'draft'") == 0, (
        "refused drafts are discarded"
    )


async def test_only_one_change_waits_at_a_time(rig: Rig) -> None:
    await rig.controller.propose(ROOT, fqdn=NEW)
    with pytest.raises(ConflictError, match="waiting"):
        await rig.controller.propose(ROOT, fqdn="other.example.test")


async def test_a_request_on_the_pending_host_gets_the_pending_public_url(rig: Rig) -> None:
    assert await rig.controller.public_url(NEW, "https://env.example.test") == (
        "https://env.example.test"
    )
    await rig.controller.propose(ROOT, fqdn=NEW, vip="192.0.2.36")
    await rig.run_change_set()
    await rig.controller.tick()
    rig.controller._forget()
    assert await rig.controller.public_url(f"{NEW}:443", "https://env.example.test") == (
        f"https://{NEW}"
    )
    assert await rig.controller.public_url(OLD, "https://env.example.test") == (
        "https://env.example.test"
    )


async def test_without_a_tls_port_a_certificate_cannot_be_chosen(pg_url: str) -> None:
    async with world(pg_url, management_data(with_tls=False)) as w:
        controller = ControllerSettings(w.db, w.service)
        with pytest.raises(FieldErrors) as caught:
            await controller.propose(ROOT, fqdn=NEW, certificate="controller-new")
        assert [e["path"] for e in caught.value.errors] == ["certificate"]


async def test_without_a_management_tenant_only_the_public_url_can_change(pg_url: str) -> None:
    async with world(pg_url) as w:  # the tenant is called acme: no management tenant
        controller = ControllerSettings(w.db, w.service)
        view = await controller.view(ROOT, env_public_url="http://localhost:8000")
        assert (
            not view.managed and view.listen is None and view.public_url == "http://localhost:8000"
        )
        with pytest.raises(FieldErrors):
            await controller.propose(ROOT, fqdn=NEW, vip="192.0.2.40")
        proposed = await controller.propose(ROOT, fqdn=NEW)
        assert (
            _d(proposed.pending)["state"] == "waiting"
            and _d(proposed.pending)["change_set_id"] is None
        )
        done = await controller.confirm(ROOT, request_host=NEW)
        assert done.public_url == f"https://{NEW}"
        with pytest.raises(FieldErrors):
            await controller.propose(ROOT, fqdn="", reset_to_default=True, vip="192.0.2.40")
        reset = await controller.propose(ROOT, reset_to_default=True)
        assert (
            _d(reset.pending)["state"] == "waiting"
            and _d(reset.pending)["after"]["public_url"] is None
        )
        back = await controller.confirm(ROOT, request_host="anything.test")
        assert back.public_url is None, "the environment's address applies again"
        shown = await controller.view(ROOT, env_public_url="http://localhost:8000")
        assert shown.public_url == "http://localhost:8000"


async def test_only_system_administrators_may_use_it(rig: Rig) -> None:
    for role in (Role.VIEWER, Role.OPERATOR, Role.TENANT_ADMIN):
        who = principal(role)
        with pytest.raises(PermissionDeniedError):
            await rig.controller.propose(who, fqdn=NEW)
        with pytest.raises(PermissionDeniedError):
            await rig.controller.view(who)


# -- the API ---------------------------------------------------------------------------------


async def test_the_api_walks_through_a_change(rig: Rig) -> None:
    rig.services.controller = rig.controller
    token, _ = await rig.services.tokens.create(ROOT, None, "sys", Role.SYSTEM_ADMIN)
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app(rig.services, public_url="https://env.example.test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://switchyard.example.test"
    ) as client:
        got = (await client.get(f"{V1}/system/controller", headers=headers)).json()
        assert got["managed"] is True and got["listen"]["vip"] == "192.0.2.20"
        assert got["certificate"]["name"] == "controller" and got["pending"] is None

        refused = await client.put(
            f"{V1}/system/controller/pending",
            json={"fqdn": NEW, "certificate": "controller-other"},
            headers=headers,
        )
        assert refused.status_code == 422
        assert refused.json()["errors"][0]["path"] == "fqdn"

        accepted = await client.put(
            f"{V1}/system/controller/pending",
            json={"fqdn": NEW, "vip": "192.0.2.37", "certificate": "controller-new"},
            headers=headers,
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["pending"]["state"] == "applying"
        assert accepted.json()["redirect_urls"] == []
        again = await client.put(
            f"{V1}/system/controller/pending", json={"fqdn": NEW}, headers=headers
        )
        assert again.status_code == 409

        await rig.run_change_set()
        await rig.controller.tick()
        waiting = (await client.get(f"{V1}/system/controller", headers=headers)).json()
        assert waiting["pending"]["state"] == "waiting" and waiting["pending"]["seconds_left"] > 0

        elsewhere = await client.post(f"{V1}/system/controller/confirm", headers=headers)
        assert elsewhere.status_code == 403, "the old address cannot confirm"
        kept = await client.post(f"{V1}/system/controller/confirm", headers=headers | {"Host": NEW})
        assert kept.status_code == 200, kept.text
        assert kept.json()["public_url"] == f"https://{NEW}" and kept.json()["pending"] is None
        nothing = await client.post(f"{V1}/system/controller/revert", headers=headers)
        assert nothing.status_code == 409


async def test_an_ip_address_can_be_the_public_address_without_a_controller_service(
    pg_url: str,
) -> None:
    from switchyard.services.bootstrap import ensure_management_tenant

    async with world(pg_url) as w:
        assert await ensure_management_tenant(w.db) is True
        assert await ensure_management_tenant(w.db) is False, "idempotent"
        controller = ControllerSettings(w.db, w.service)
        view = await controller.view(
            ROOT, env_public_url="https://172.30.0.5:8443", reached_at="https://[2001:db8::5]:8443"
        )
        assert view.management_tenant and not view.managed
        assert view.reached_at == "https://[2001:db8::5]:8443"
        with pytest.raises(FieldErrors):
            await controller.propose(ROOT, fqdn="not an address!")
        proposed = await controller.propose(ROOT, fqdn="2001:db8::5")
        assert _d(proposed.pending)["after"]["public_url"] == "https://[2001:db8::5]"
        with pytest.raises(PermissionDeniedError):
            await controller.confirm(ROOT, request_host="172.30.0.5:8443")
        done = await controller.confirm(ROOT, request_host="[2001:db8::5]:8443")
        assert done.public_url == "https://[2001:db8::5]" and done.fqdn == "2001:db8::5"
        again = await controller.propose(ROOT, fqdn="172.30.0.5")
        assert _d(again.pending)["after"]["public_url"] == "https://172.30.0.5"


async def test_with_a_controller_service_an_ip_is_not_a_valid_fqdn(rig: Rig) -> None:
    with pytest.raises(FieldErrors):
        await rig.controller.propose(ROOT, fqdn="192.0.2.99")

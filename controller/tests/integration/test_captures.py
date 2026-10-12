# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Packet captures on engines (ADR 0194): request, run through the agent, download, expire."""

from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from switchyard.adapters.agent import CaptureResult, CaptureSpec
from switchyard.api.app import build_services, create_app
from switchyard.db.ops_models import JobRow, ServiceEngineRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.services.captures import (
    PACKET_CAPTURE_JOB,
    RETENTION,
    PacketCaptures,
    expire,
)
from switchyard.services.context import Principal, utcnow
from switchyard.services.engines import EngineService
from switchyard.services.errors import (
    ConflictError,
    FieldErrors,
    NotFoundError,
    PermissionDeniedError,
)
from tests.integration.test_pipeline_routing import healthy_vrrp, prime_bundles, vrrp_data
from tests.pipeline_env import GROUP, TENANT, World, engine_record, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")

PCAP = b"\xd4\xc3\xb2\xa1" + b"x" * 10_000
ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)


async def register(w: World, *names: str) -> None:
    """Rows for the fake engines of the world, with the ids its directory uses."""
    async with admin_session(w.db.admin) as s:
        for name in names:
            record = engine_record(name, agent=True, mgmt_ip="10.30.0.11")
            s.add(
                ServiceEngineRow(
                    id=record.id,
                    tenant_id=TENANT,
                    se_group_id=GROUP,
                    name=name,
                    dataplane_url=record.dataplane_url,
                    agent_url=record.agent_url,
                    mgmt_ip=record.mgmt_ip,
                )
            )


class Rig:
    def __init__(self, w: World) -> None:
        self.w = w
        self.captures = PacketCaptures(w.db, w.pipeline._directory)
        self.admin = principal()

    async def engine_id(self, name: str = "se-a"):  # type: ignore[no-untyped-def]
        rows = await EngineService(self.w.db).list_engines(self.admin, TENANT)
        return next(e.id for e in rows if e.name == name)

    async def ask(self, **spec: object):  # type: ignore[no-untyped-def]
        return await self.captures.request(
            self.admin,
            TENANT,
            await self.engine_id(),
            CaptureSpec(**spec),  # type: ignore[arg-type]
        )

    async def run(self, capture: dict) -> dict:  # type: ignore[type-arg]
        return await self.captures.run(TENANT, capture["id"])


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url, vrrp_data()) as w:
        w.use_engines("se-a", "se-b", agents=True)
        await register(w, "se-a", "se-b")
        await w.prime()
        await prime_bundles(w)
        healthy_vrrp(w)
        w.agents["se-a"].capture_data = PCAP
        w.agents["se-a"].capture_result = CaptureResult(packets=42)
        yield Rig(w)


async def test_a_capture_is_queued_run_and_downloadable(rig: Rig) -> None:
    made = await rig.ask(interface="any", protocol="tcp", host="192.0.2.20", port=443)
    assert (made["state"], made["engine"], made["requested_by"]) == ("queued", "se-a", "user:alice")
    async with admin_session(rig.w.db.admin) as s:
        (job,) = (
            (await s.execute(select(JobRow).where(JobRow.kind == PACKET_CAPTURE_JOB)))
            .scalars()
            .all()
        )
    assert job.payload["capture_id"] == str(made["id"])

    assert (await rig.run(made)) == {"state": "done", "bytes": len(PCAP), "packets": 42}
    done = await rig.captures.get(rig.admin, TENANT, made["id"])
    assert (done["state"], done["packets"], done["size_bytes"]) == ("done", 42, len(PCAP))
    assert done["spec"]["port"] == 443 and done["finished_at"] is not None
    sent = rig.w.agents["se-a"].captures
    assert sent == [CaptureSpec(protocol="tcp", host="192.0.2.20", port=443)]

    name, data = await rig.captures.download(rig.admin, TENANT, made["id"])
    assert data == PCAP and name.startswith("se-a-") and name.endswith(".pcap")
    async with admin_session(rig.w.db.admin) as s:
        actions = (
            (await s.execute(text("SELECT action FROM audit_event WHERE action LIKE 'capture.%'")))
            .scalars()
            .all()
        )
    assert sorted(actions) == ["capture.downloaded", "capture.requested"]


async def test_fields_are_validated_one_by_one(rig: Rig) -> None:
    with pytest.raises(FieldErrors) as caught:
        await rig.ask(
            interface="eth0;reboot", protocol="ip", host="not-an-ip", port=70000, duration_s=600
        )
    paths = {e["path"] for e in caught.value.errors}
    assert {"interface", "protocol", "host", "port", "duration_s"} <= paths
    with pytest.raises(FieldErrors):
        await rig.ask(protocol="icmp", port=80)
    with pytest.raises(FieldErrors):
        await rig.ask(max_packets=20_001)


async def test_one_capture_at_a_time_per_engine(rig: Rig) -> None:
    first = await rig.ask()
    with pytest.raises(ConflictError, match="in progress"):
        await rig.ask()
    await rig.run(first)  # finished: the engine is free again
    await rig.ask()


async def test_only_a_tenant_admin_may_capture_or_download(rig: Rig) -> None:
    made = await rig.ask()
    await rig.run(made)
    for role in (Role.VIEWER, Role.OPERATOR):
        who = principal(role)
        with pytest.raises(PermissionDeniedError):
            await rig.captures.request(who, TENANT, await rig.engine_id(), CaptureSpec())
        with pytest.raises(PermissionDeniedError):
            await rig.captures.download(who, TENANT, made["id"])
        with pytest.raises(PermissionDeniedError):
            await rig.captures.list_captures(who, TENANT)


async def test_another_tenant_cannot_see_a_capture(rig: Rig) -> None:
    made = await rig.ask()
    other = principal(Role.TENANT_ADMIN, tenant=__import__("uuid").uuid4())
    with pytest.raises(PermissionDeniedError):
        await rig.captures.get(other, TENANT, made["id"])
    # and a tenant session cannot read it by id either (row-level security)
    assert [c["id"] for c in await rig.captures.list_captures(rig.admin, TENANT)] == [made["id"]]


async def test_a_failure_of_the_agent_is_recorded_on_the_capture(rig: Rig) -> None:
    made = await rig.ask()
    rig.w.agents["se-a"].fail_next = "capture"
    result = await rig.run(made)
    assert result["state"] == "failed"
    failed = await rig.captures.get(rig.admin, TENANT, made["id"])
    assert failed["state"] == "failed" and "injected failure" in failed["error"]


async def test_a_refusal_by_the_agent_is_shown_and_keeps_no_file(rig: Rig) -> None:
    rig.w.agents["se-a"].capture_data = b""
    rig.w.agents["se-a"].capture_result = CaptureResult(error="this engine has no interface eth9")
    made = await rig.ask(interface="eth9")
    await rig.run(made)
    refused = await rig.captures.get(rig.admin, TENANT, made["id"])
    assert (refused["state"], refused["size_bytes"]) == ("failed", 0)
    assert "no interface" in refused["error"]
    with pytest.raises(NotFoundError):
        await rig.captures.download(rig.admin, TENANT, made["id"])


async def test_a_capture_cut_short_by_the_size_limit_says_so(rig: Rig) -> None:
    rig.w.agents["se-a"].capture_result = CaptureResult(packets=9, truncated=True)
    made = await rig.ask()
    await rig.run(made)
    assert (await rig.captures.get(rig.admin, TENANT, made["id"]))["truncated"] is True


async def test_captures_are_deleted_after_the_retention(rig: Rig) -> None:
    made = await rig.ask()
    await rig.run(made)
    assert await expire(rig.w.db, utcnow() + RETENTION - timedelta(minutes=1)) == 0
    assert await expire(rig.w.db, utcnow() + RETENTION + timedelta(minutes=1)) == 1
    with pytest.raises(NotFoundError):
        await rig.captures.get(rig.admin, TENANT, made["id"])


async def test_the_api_serves_the_file_as_a_download(pg_url: str) -> None:
    async with world(pg_url, vrrp_data()) as w:
        w.use_engines("se-a", "se-b", agents=True)
        await register(w, "se-a", "se-b")
        await w.prime()
        await prime_bundles(w)
        healthy_vrrp(w)
        w.agents["se-a"].capture_data = PCAP
        services = build_services(w.db, w.pipeline._directory)
        token, _ = await services.tokens.create(ROOT, TENANT, "admin", Role.TENANT_ADMIN)
        viewer, _ = await services.tokens.create(ROOT, TENANT, "viewer", Role.VIEWER)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(services)), base_url="http://api"
        ) as client:
            h = {"Authorization": f"Bearer {token}"}
            engine = next(
                e["id"]
                for e in (await client.get("/api/v1/tenants/acme/engines", headers=h)).json()
                if e["name"] == "se-a"
            )
            made = await client.post(
                f"/api/v1/tenants/acme/engines/{engine}/captures",
                json={"protocol": "udp", "port": 53, "duration_s": 5},
                headers=h,
            )
            assert made.status_code == 202, made.text
            bad = await client.post(
                f"/api/v1/tenants/acme/engines/{engine}/captures",
                json={"interface": "eth0;reboot"},
                headers=h,
            )
            assert bad.status_code == 422
            await services.captures.run(TENANT, __import__("uuid").UUID(made.json()["id"]))
            got = await client.get(f"/api/v1/tenants/acme/captures/{made.json()['id']}", headers=h)
            assert got.json()["state"] == "done"
            pcap = await client.get(
                f"/api/v1/tenants/acme/captures/{made.json()['id']}/pcap", headers=h
            )
            assert pcap.content == PCAP
            assert pcap.headers["content-type"] == "application/vnd.tcpdump.pcap"
            assert 'filename="se-a-' in pcap.headers["content-disposition"]
            denied = await client.get(
                f"/api/v1/tenants/acme/captures/{made.json()['id']}/pcap",
                headers={"Authorization": f"Bearer {viewer}"},
            )
            assert denied.status_code == 403

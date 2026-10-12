"""DNS address records published from virtual services (ADR 0193), against a real database."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import update

from switchyard.adapters.cloudflare import FakeCloudflare
from switchyard.adapters.dns.base import DnsProvider
from switchyard.adapters.secrets import MemorySecrets
from switchyard.db.models import VirtualServiceRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.services.dns_providers import DnsProviders
from switchyard.services.dns_records import DnsRecordSync
from switchyard.services.errors import PermissionDeniedError
from tests.pipeline_env import TENANT, VS, World, principal, world, world_data

pytestmark = pytest.mark.usefixtures("owner_engine")

NAMES = {"names": ["app.example.test", "www.example.test"], "ttl": 120}


class FakeProviders(DnsProviders):
    """Hands out one shared fake Cloudflare instead of building a client."""

    def __init__(self, w: World, cloudflare: FakeCloudflare) -> None:
        super().__init__(w.db, MemorySecrets())
        self.cloudflare = cloudflare

    async def build(self, row: Any) -> DnsProvider:
        return self.cloudflare  # type: ignore[return-value]


class Rig:
    def __init__(self, w: World, cf: FakeCloudflare) -> None:
        self.w, self.cf = w, cf
        self.providers = FakeProviders(w, cf)
        self.sync = DnsRecordSync(w.db, self.providers)
        self.admin = principal()

    async def credential(self, **extra: Any) -> None:
        await self.providers.create(
            self.admin,
            TENANT,
            name="cf",
            type_="cloudflare",
            zones=["example.test"],
            config={},
            credentials={"api_token": "t"},
            **extra,
        )

    async def set_dns(self, dns: dict[str, Any] | None, **columns: Any) -> None:
        async with admin_session(self.w.db.admin) as session:
            await session.execute(
                update(VirtualServiceRow)
                .where(VirtualServiceRow.id == VS)
                .values(dns=dns, **columns)
            )


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url, world_data()) as w:
        r = Rig(w, FakeCloudflare())
        await r.credential()
        yield r


def records(r: Rig) -> dict[str, tuple[str, str, int, bool]]:
    return {n: (c, t, ttl, p) for n, t, c, ttl, p in r.cf.addresses.values()}


async def test_a_service_with_names_gets_records(rig: Rig) -> None:
    await rig.set_dns(NAMES)
    result = await rig.sync.sync(rig.admin, TENANT)
    assert result == {"written": 2, "deleted": 0, "failed": 0}
    assert records(rig) == {
        "app.example.test": ("192.0.2.20", "A", 120, False),
        "www.example.test": ("192.0.2.20", "A", 120, False),
    }
    listed = await rig.sync.list_records(rig.admin, TENANT)
    assert [(r["hostname"], r["state"], r["virtual_service"]) for r in listed] == [
        ("app.example.test", "published", "web"),
        ("www.example.test", "published", "web"),
    ]


async def test_a_second_run_changes_nothing(rig: Rig) -> None:
    await rig.set_dns(NAMES)
    await rig.sync.sync(rig.admin, TENANT)
    assert await rig.sync.sync_tenant(TENANT) == {"written": 0, "deleted": 0, "failed": 0}


async def test_a_moved_vip_updates_the_record_in_place(rig: Rig) -> None:
    await rig.set_dns({"names": ["app.example.test"]})
    await rig.sync.sync_tenant(TENANT)
    before = set(rig.cf.addresses)
    await rig.set_dns({"names": ["app.example.test"]}, vip="192.0.2.99")
    assert (await rig.sync.sync_tenant(TENANT))["written"] == 1
    assert set(rig.cf.addresses) == before
    assert records(rig)["app.example.test"][0] == "192.0.2.99"


async def test_a_removed_name_or_block_deletes_only_our_records(rig: Rig) -> None:
    rig.cf.addresses["foreign"] = ("other.example.test", "A", "198.51.100.1", 300, False)
    await rig.set_dns(NAMES)
    await rig.sync.sync_tenant(TENANT)
    await rig.set_dns({"names": ["app.example.test"]})
    assert (await rig.sync.sync_tenant(TENANT))["deleted"] == 1
    await rig.set_dns(None)
    assert (await rig.sync.sync_tenant(TENANT))["deleted"] == 1
    assert list(rig.cf.addresses) == ["foreign"]
    assert await rig.sync.list_records(rig.admin, TENANT) == []


async def test_a_disabled_service_loses_its_records(rig: Rig) -> None:
    await rig.set_dns(NAMES)
    await rig.sync.sync_tenant(TENANT)
    await rig.set_dns(NAMES, enabled=False)
    assert (await rig.sync.sync_tenant(TENANT))["deleted"] == 2
    assert rig.cf.addresses == {}


async def test_a_foreign_record_with_another_value_is_never_overwritten(rig: Rig) -> None:
    rig.cf.addresses["theirs"] = ("app.example.test", "A", "198.51.100.1", 300, False)
    await rig.set_dns({"names": ["app.example.test"]})
    assert (await rig.sync.sync_tenant(TENANT))["failed"] == 1
    assert rig.cf.addresses["theirs"][2] == "198.51.100.1"
    (row,) = await rig.sync.list_records(rig.admin, TENANT)
    assert row["state"] == "error"
    assert "another value" in row["last_error"]


async def test_a_failed_push_is_retried_by_the_next_run(rig: Rig) -> None:
    await rig.set_dns({"names": ["app.example.test"]})
    rig.cf.fail_on.add("upsert_address")
    assert (await rig.sync.sync_tenant(TENANT))["failed"] == 1
    assert (await rig.sync.sync_tenant(TENANT))["written"] == 1
    (row,) = await rig.sync.list_records(rig.admin, TENANT)
    assert row["state"] == "published"
    assert row["last_error"] is None


async def test_the_scheduled_run_covers_every_tenant(rig: Rig) -> None:
    await rig.set_dns({"names": ["app.example.test"], "proxied": True})
    assert (await rig.sync.run())["written"] == 1
    assert records(rig)["app.example.test"][3] is True


async def test_a_viewer_can_read_but_not_sync(rig: Rig) -> None:
    viewer = principal(Role.VIEWER)
    assert await rig.sync.list_records(viewer, TENANT) == []
    with pytest.raises(PermissionDeniedError, match="tenant-admin"):
        await rig.sync.sync(viewer, TENANT)


async def test_a_credential_without_address_support_reports_an_error(pg_url: str) -> None:
    async with world(pg_url, world_data()) as w:
        r = Rig(w, FakeCloudflare())
        await r.credential()

        class TxtOnly:
            async def close(self) -> None: ...

        r.providers.cloudflare = TxtOnly()  # type: ignore[assignment]
        await r.set_dns({"names": ["app.example.test"]})
        assert (await r.sync.sync_tenant(TENANT))["failed"] == 1
        (row,) = await r.sync.list_records(r.admin, TENANT)
        assert "cannot publish address records" in row["last_error"]

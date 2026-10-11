"""Display names for principal labels."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from switchyard.api.app import build_services, create_app
from switchyard.db.auth_models import IdentityProviderRow, UserAccountRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from switchyard.services.principals import display_names, fallback
from tests.pipeline_env import GROUP, TENANT, World, add_host_route, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)


def test_labels_without_a_lookup_are_still_named() -> None:
    assert fallback("token:ci").display_name == "ci (API token)"
    assert fallback("local:admin").display_name == "admin (break-glass)"
    assert fallback("login:okta").display_name == "sign-in via okta"
    assert fallback("user:abc").display_name == "abc"
    assert fallback("").display_name == "unknown"


async def add_users(w: World) -> None:
    async with admin_session(w.db.admin) as s:
        provider = IdentityProviderRow(
            id=uuid.uuid4(),
            name="corp",
            protocol="oidc",
            issuer_url="https://idp.example.test",
            client_id="switchyard",
        )
        s.add(provider)
        await s.flush()
        for subject, name in (("sub-alice", "Alice Admin"), ("sub-eve", "Eve Elsewhere")):
            s.add(
                UserAccountRow(
                    id=uuid.uuid4(), provider_id=provider.id, subject=subject, username=name
                )
            )


@pytest_asyncio.fixture
async def setup(pg_url: str) -> AsyncIterator[tuple[World, httpx.AsyncClient, dict[str, str]]]:
    async with world(pg_url) as w:
        await add_users(w)
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        services = build_services(w.db, directory)
        alice = Principal(
            actor="user:sub-alice", role=Role.OPERATOR, tenant_ids=frozenset({TENANT})
        )
        assert (await w.run_change([add_host_route("one.example.test")], who=alice)).ok
        token, _ = await services.tokens.create(ROOT, TENANT, "viewer", Role.VIEWER)
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
            yield w, c, {"Authorization": f"Bearer {token}"}


async def test_users_in_the_tenants_history_get_their_provider_name(
    setup: tuple[World, httpx.AsyncClient, dict[str, str]],
) -> None:
    w, _, _ = setup
    got = await display_names(
        w.db, ["user:sub-alice", "token:ci", "user:sub-alice", "user:sub-eve"], tenant_id=TENANT
    )
    assert [(n.actor, n.kind, n.display_name) for n in got] == [
        ("user:sub-alice", "user", "Alice Admin"),
        ("token:ci", "token", "ci (API token)"),
        ("user:sub-eve", "user", "sub-eve"),  # never seen in this tenant: label only
    ]
    everyone = await display_names(w.db, ["user:sub-eve"])
    assert everyone[0].display_name == "Eve Elsewhere"


async def test_api_resolves_in_the_tenant_and_for_system_admins(
    setup: tuple[World, httpx.AsyncClient, dict[str, str]],
) -> None:
    _, c, h = setup
    r = await c.get(
        "/api/v1/tenants/acme/principals?actor=user:sub-alice&actor=system:drift_detector",
        headers=h,
    )
    assert r.status_code == 200
    assert [(x["actor"], x["display_name"]) for x in r.json()] == [
        ("user:sub-alice", "Alice Admin"),
        ("system:drift_detector", "drift detector (system)"),
    ]
    assert (await c.get("/api/v1/tenants/acme/principals", headers=h)).json() == []
    assert (await c.get("/api/v1/principals?actor=user:sub-eve", headers=h)).status_code == 403
    many = "&".join(f"actor=token:t{i}" for i in range(101))
    assert (await c.get(f"/api/v1/tenants/acme/principals?{many}", headers=h)).status_code == 422

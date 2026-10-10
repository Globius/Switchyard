"""SAML login end to end: AuthnRequest, signed response to the ACS, session, audit, replay."""

import base64
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from lxml import etree
from sqlalchemy import text

from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.idp import IdentityProviderAdmin
from switchyard.services.oidc import OidcClient
from switchyard.services.sessions import SessionService
from tests.pipeline_env import World, world
from tests.saml_idp import IdpKeys, encode, make_idp, response_xml, sign

pytestmark = pytest.mark.usefixtures("owner_engine")

IDP_ENTITY = "https://idp.example.test/entity"
SSO = "https://idp.example.test/sso"
BASE = "https://switchyard.example.test"
V1 = "/api/v1"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Rig:
    w: World
    idp: IdpKeys
    clock: Clock
    client: httpx.AsyncClient
    provider_id: UUID

    @property
    def acs(self) -> str:
        return f"{BASE}{V1}/auth/callback/{self.provider_id}"

    @property
    def sp_entity(self) -> str:
        return f"{BASE}{V1}/auth/saml/{self.provider_id}"

    async def start(self) -> tuple[str, str]:
        """Begin a login; returns ``(request_id, relay_state)`` read from the redirect."""
        started = await self.client.get(f"{V1}/auth/login/corp", params={"return_to": "/t/acme"})
        assert started.status_code == 302, started.text
        location = started.headers["location"]
        assert location.startswith(SSO)
        query = parse_qs(urlparse(location).query)
        xml = zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15)
        root = etree.fromstring(xml)
        assert root.get("AssertionConsumerServiceURL") == self.acs
        return root.get("ID") or "", query["RelayState"][0]

    def response(self, request_id: str, **kw: object) -> str:
        args: dict[str, object] = {
            "audience": self.sp_entity,
            "recipient": self.acs,
            "destination": self.acs,
            "issuer": IDP_ENTITY,
            "now": self.clock.now,
        } | kw
        return encode(sign(response_xml(request_id, **args), self.idp))  # type: ignore[arg-type]

    async def post(self, saml_response: str, relay_state: str) -> httpx.Response:
        return await self.client.post(
            f"{V1}/auth/callback/{self.provider_id}",
            content=urlencode({"SAMLResponse": saml_response, "RelayState": relay_state}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        clock, idp = Clock(), make_idp()
        sessions = SessionService(
            w.db,
            OidcClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
            MemorySecrets({}),
            clock=clock,
        )
        admin = IdentityProviderAdmin(w.db)
        provider_id = await admin.add_saml_provider(
            "corp",
            idp_entity_id=IDP_ENTITY,
            sso_url=SSO,
            signing_certificates=[idp.cert_pem],
        )
        await admin.map_group("corp", "sy-admins", Role.TENANT_ADMIN, tenant="acme")
        services = build_services(w.db, StaticEngineDirectory({}), sessions=sessions)
        app = create_app(services, public_url=BASE)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as client:
            yield Rig(w, idp, clock, client, provider_id)


async def count(rig: Rig, sql: str) -> int:
    async with admin_session(rig.w.db.admin) as s:
        return int((await s.execute(text(sql))).scalar_one())


async def failed(rig: Rig) -> int:
    return await count(rig, "SELECT count(*) FROM audit_event WHERE action='auth.login_failed'")


async def test_signed_response_logs_in_with_the_mapped_role(rig: Rig) -> None:
    providers = (await rig.client.get(f"{V1}/auth/providers")).json()["providers"]
    assert [(p["name"], p["protocol"]) for p in providers] == [("corp", "saml")]
    request_id, relay = await rig.start()
    done = await rig.post(rig.response(request_id), relay)
    assert done.status_code == 302 and done.headers["location"] == "/t/acme"
    assert "httponly" in done.headers["set-cookie"].lower()
    me = (await rig.client.get(f"{V1}/me")).json()
    assert me["name"] == "alice" and me["email"] == "alice@example.test"
    assert {t["name"]: t["role"] for t in me["tenants"]} == {"acme": "tenant-admin"}
    assert await count(rig, "SELECT count(*) FROM audit_event WHERE action='auth.login'") == 1


async def test_a_replayed_response_is_refused_and_audited(rig: Rig) -> None:
    request_id, relay = await rig.start()
    saml_response = rig.response(request_id)
    assert (await rig.post(saml_response, relay)).headers["location"] == "/t/acme"
    # The state is single use, so the replay is refused even before the assertion cache.
    again = await rig.post(saml_response, relay)
    assert again.headers["location"] == "/login?error=expired"
    # A fresh login that reuses the same assertion id is caught by the replay cache.
    request2, relay2 = await rig.start()
    reused = await rig.post(rig.response(request2), relay2)
    assert reused.headers["location"] == "/login?error=provider_error"
    assert await failed(rig) == 2


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"in_response_to": "_other"}, "provider_error"),
        ({"audience": "https://other.test"}, "provider_error"),
        ({"recipient": "https://evil.test/acs"}, "provider_error"),
        ({"issuer": "https://evil.test"}, "provider_error"),
        ({"groups": ("nobody",)}, "no_access"),
    ],
)
async def test_bad_responses_redirect_with_an_error_code(
    rig: Rig, override: dict[str, object], code: str
) -> None:
    request_id, relay = await rig.start()
    done = await rig.post(rig.response(request_id, **override), relay)
    assert done.status_code == 302 and done.headers["location"] == f"/login?error={code}"
    assert "set-cookie" not in done.headers
    assert (await rig.client.get(f"{V1}/me")).status_code == 401


async def test_unsigned_expired_and_idp_initiated_responses_are_refused(rig: Rig) -> None:
    request_id, relay = await rig.start()
    unsigned = encode(
        response_xml(
            request_id,
            audience=rig.sp_entity,
            recipient=rig.acs,
            destination=rig.acs,
            now=rig.clock.now,
        )
    )
    assert (await rig.post(unsigned, relay)).headers["location"] == "/login?error=provider_error"

    request_id, relay = await rig.start()
    late = rig.response(request_id, not_on_or_after=rig.clock.now - timedelta(minutes=10))
    assert (await rig.post(late, relay)).headers["location"] == "/login?error=provider_error"

    # IdP-initiated: no stored login state exists for this relay state.
    pushed = rig.response("_never_asked")
    assert (await rig.post(pushed, "made-up")).headers["location"] == "/login?error=expired"
    assert await failed(rig) == 3


async def test_the_state_expires(rig: Rig) -> None:
    request_id, relay = await rig.start()
    rig.clock.now += timedelta(minutes=11)
    done = await rig.post(rig.response(request_id), relay)
    assert done.headers["location"] == "/login?error=expired"


async def test_metadata_lists_the_acs_and_can_be_downloaded(rig: Rig) -> None:
    r = await rig.client.get(f"{V1}/auth/saml/corp/metadata")
    assert r.status_code == 200 and r.headers["content-type"].startswith(
        "application/samlmetadata+xml"
    )
    root = etree.fromstring(r.content)
    ns = {"md": "urn:oasis:names:tc:SAML:2.0:metadata"}
    assert root.get("entityID") == rig.sp_entity
    acs = root.find("md:SPSSODescriptor/md:AssertionConsumerService", ns)
    assert acs is not None and acs.get("Location") == rig.acs
    assert "content-disposition" not in r.headers
    dl = await rig.client.get(f"{V1}/auth/saml/corp/metadata", params={"download": "1"})
    assert "attachment" in dl.headers["content-disposition"]
    assert (await rig.client.get(f"{V1}/auth/saml/nope/metadata")).status_code == 404


async def test_the_oidc_callback_path_does_not_accept_saml_providers(rig: Rig) -> None:
    r = await rig.client.get(f"{V1}/auth/callback/corp", params={"code": "x", "state": "y"})
    assert r.status_code == 302 and r.headers["location"].startswith("/login?error=")

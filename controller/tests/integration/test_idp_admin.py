"""G1-3, G1-4, G1-7: identity-provider API, test sign-in, SAML metadata/signing, break-glass, tokens."""

import base64
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from lxml import etree
from sqlalchemy import text

from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.break_glass import BreakGlassService
from switchyard.services.identity_providers import IdentityProviderService
from switchyard.services.oidc import OidcClient
from switchyard.services.sessions import SessionService, totp_code
from tests.integration.test_login import ISSUER, FakeIdp
from tests.pipeline_env import TENANT, World, principal, world
from tests.saml_idp import IdpKeys, encode, make_idp, response_xml, sign

pytestmark = pytest.mark.usefixtures("owner_engine")
V1 = "/api/v1"
BASE = "https://switchyard.example.test"
ROOT = principal(Role.SYSTEM_ADMIN, "root")
IDP_ENTITY = "https://idp.example.test/entity"
SSO = "https://idp.example.test/sso"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Rig:
    w: World
    oidc: FakeIdp
    clock: Clock
    secrets: MemorySecrets
    services: ApiServices
    client: httpx.AsyncClient
    admin: dict[str, str]
    metadata: dict[str, str]  # url -> document served to the metadata fetcher

    async def create_oidc(self, name: str = "authentik", **extra: Any) -> dict[str, Any]:
        body = {
            "protocol": "oidc",
            "name": name,
            "issuer_url": ISSUER,
            "client_id": "switchyard",
            "client_secret": "s3cret-value",
        } | extra
        made = await self.client.post(f"{V1}/identity-providers", json=body, headers=self.admin)
        assert made.status_code == 201, made.text
        return made.json()  # type: ignore[no-any-return]

    async def sql(self, query: str) -> int:
        async with admin_session(self.w.db.admin) as s:
            return int((await s.execute(text(query))).scalar_one())


def idp_metadata(keys: IdpKeys, entity: str = IDP_ENTITY, sso: str = SSO) -> str:
    cert = "".join(line for line in keys.cert_pem.splitlines() if "-----" not in line)
    return (
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f'entityID="{entity}"><md:IDPSSODescriptor '
        'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        '<md:KeyDescriptor use="signing"><ds:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">'
        f"<ds:X509Data><ds:X509Certificate>{cert}</ds:X509Certificate></ds:X509Data></ds:KeyInfo>"
        "</md:KeyDescriptor>"
        '<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        'Location="https://idp.example.test/post"/>'
        '<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
        f'Location="{sso}"/></md:IDPSSODescriptor></md:EntityDescriptor>'
    )


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        clock, oidc = Clock(), FakeIdp()
        secrets = MemorySecrets({"kv/break-glass/unused": "x"})
        documents: dict[str, str] = {}

        def serve_metadata(request: httpx.Request) -> httpx.Response:
            body = documents.get(str(request.url))
            return httpx.Response(200, text=body) if body else httpx.Response(404)

        oidc_client = OidcClient(transport=httpx.MockTransport(oidc.handler))
        sessions = SessionService(w.db, oidc_client, secrets, clock=clock)
        providers = IdentityProviderService(
            w.db,
            oidc_client,
            secrets,
            secrets,
            http=httpx.AsyncClient(transport=httpx.MockTransport(serve_metadata)),
            clock=clock,
        )
        services = build_services(
            w.db,
            StaticEngineDirectory({}),
            secrets,
            sessions=sessions,
            identity_providers=providers,
            break_glass=BreakGlassService(w.db, secrets, secrets, clock=clock),
        )
        plaintext, _ = await services.tokens.create(ROOT, None, "sys", Role.SYSTEM_ADMIN)
        app = create_app(services, public_url=BASE)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as client:
            yield Rig(
                w,
                oidc,
                clock,
                secrets,
                services,
                client,
                {"Authorization": f"Bearer {plaintext}"},
                documents,
            )


# -- OIDC providers ---------------------------------------------------------------------------


async def test_oidc_provider_crud_never_returns_the_secret(rig: Rig) -> None:
    made = await rig.create_oidc()
    assert made["protocol"] == "oidc" and made["version"] == 1 and made["warnings"] == []
    assert made["client_secret_ref"] == "kv/idp/authentik" and made["client_secret_set"] is True
    assert "s3cret-value" not in str(made) and "client_secret" not in made
    assert await rig.secrets.get("kv/idp/authentik") == "s3cret-value", "stored in the backend"

    url = f"{V1}/identity-providers/{made['id']}"
    got = await rig.client.get(url, headers=rig.admin)
    assert got.headers["etag"] == '"1"' and "s3cret" not in got.text
    listing = (await rig.client.get(f"{V1}/identity-providers", headers=rig.admin)).json()
    assert [p["name"] for p in listing] == ["authentik"]

    no_header = await rig.client.patch(
        url, json={"protocol": "oidc", "scopes": "openid"}, headers=rig.admin
    )
    assert no_header.status_code == 428
    stale = await rig.client.patch(
        url, json={"protocol": "oidc"}, headers=rig.admin | {"If-Match": '"9"'}
    )
    assert stale.status_code == 412
    changed = await rig.client.patch(
        url,
        json={
            "protocol": "oidc",
            "scopes": "openid email",
            "client_secret": "rotated",
            "enabled": False,
        },
        headers=rig.admin | {"If-Match": '"1"'},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == 2 and changed.json()["enabled"] is False
    assert await rig.secrets.get("kv/idp/authentik") == "rotated"
    assert "rotated" not in changed.text
    wrong_protocol = await rig.client.patch(
        url, json={"protocol": "saml"}, headers=rig.admin | {"If-Match": '"2"'}
    )
    assert wrong_protocol.status_code == 422
    assert wrong_protocol.json()["errors"][0]["path"] == "protocol"

    assert (
        await rig.client.delete(url, headers=rig.admin | {"If-Match": '"2"'})
    ).status_code == 204
    assert (await rig.client.get(url, headers=rig.admin)).status_code == 404
    audited = await rig.sql(
        "SELECT count(*) FROM audit_event WHERE action IN ('idp.created','idp.updated','idp.deleted')"
    )
    assert audited == 3
    leaked = await rig.sql(
        "SELECT count(*) FROM audit_event "
        "WHERE diff::text LIKE '%rotated%' OR diff::text LIKE '%s3cret-value%'"
    )
    assert leaked == 0, "secrets never reach the audit log"


async def test_validation_errors_name_the_field(rig: Rig) -> None:
    bad = await rig.client.post(
        f"{V1}/identity-providers",
        json={
            "protocol": "oidc",
            "name": "Bad Name",
            "issuer_url": "http://sso.example.test",
            "client_id": "x",
            "groups_claim": "has space",
            "scopes": "profile",
            "client_secret": "a",
            "client_secret_ref": "kv/x",
        },
        headers=rig.admin,
    )
    assert bad.status_code == 422
    paths = {e["path"] for e in bad.json()["errors"]}
    assert paths >= {"name", "issuer_url", "groups_claim", "scopes", "client_secret"}
    missing = await rig.client.post(
        f"{V1}/identity-providers", json={"protocol": "oidc", "name": "x"}, headers=rig.admin
    )
    assert {e["path"] for e in missing.json()["errors"]} == {"issuer_url", "client_id"}
    foreign = await rig.client.post(
        f"{V1}/identity-providers",
        json={"protocol": "saml", "name": "x", "sso_url": "https://a", "client_id": "nope"},
        headers=rig.admin,
    )
    assert foreign.status_code == 422, "fields of the other protocol are refused"


async def test_an_unreachable_discovery_document_is_a_warning(rig: Rig) -> None:
    made = await rig.create_oidc("elsewhere", issuer_url="https://other.example.test")
    assert made["warnings"] and "discovery" in made["warnings"][0]
    again = await rig.client.post(
        f"{V1}/identity-providers",
        json={
            "protocol": "oidc",
            "name": "elsewhere",
            "issuer_url": ISSUER,
            "client_id": "x",
        },
        headers=rig.admin,
    )
    assert again.status_code == 409


async def test_only_system_administrators_manage_providers(rig: Rig) -> None:
    plaintext, _ = await rig.services.tokens.create(ROOT, TENANT, "ta", Role.TENANT_ADMIN)
    denied = await rig.client.get(
        f"{V1}/identity-providers", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert denied.status_code == 403 and denied.json()["required_role"] == "system-admin"


# -- SAML providers ---------------------------------------------------------------------------


async def test_saml_provider_from_metadata_xml_and_url(rig: Rig) -> None:
    keys = make_idp()
    by_xml = await rig.client.post(
        f"{V1}/identity-providers",
        json={"protocol": "saml", "name": "corp", "idp_metadata_xml": idp_metadata(keys)},
        headers=rig.admin,
    )
    assert by_xml.status_code == 201, by_xml.text
    body = by_xml.json()
    assert (body["idp_entity_id"], body["sso_url"]) == (IDP_ENTITY, SSO)
    assert len(body["signing_certificates"]) == 1 and body["sign_requests"] is False
    assert body["acs_url"] == f"{BASE}{V1}/auth/callback/{body['id']}"
    assert body["sp_entity_id"] == f"{BASE}{V1}/auth/saml/{body['id']}"

    rig.metadata["https://idp2.example.test/metadata"] = idp_metadata(
        make_idp(), "https://idp2.example.test/e", "https://idp2.example.test/sso"
    )
    by_url = await rig.client.post(
        f"{V1}/identity-providers",
        json={
            "protocol": "saml",
            "name": "corp2",
            "idp_metadata_url": "https://idp2.example.test/metadata",
        },
        headers=rig.admin,
    )
    assert by_url.status_code == 201, by_url.text
    assert by_url.json()["idp_entity_id"] == "https://idp2.example.test/e"

    broken = await rig.client.post(
        f"{V1}/identity-providers",
        json={"protocol": "saml", "name": "bad", "idp_metadata_xml": "<EntityDescriptor/>"},
        headers=rig.admin,
    )
    assert broken.status_code == 422 and broken.json()["errors"][0]["path"] == "idp_metadata_xml"
    nowhere = await rig.client.post(
        f"{V1}/identity-providers",
        json={"protocol": "saml", "name": "bad", "idp_metadata_url": "https://nowhere.test/m"},
        headers=rig.admin,
    )
    assert nowhere.status_code == 422
    plain_http = await rig.client.post(
        f"{V1}/identity-providers",
        json={"protocol": "saml", "name": "bad", "idp_metadata_url": "http://idp.test/m"},
        headers=rig.admin,
    )
    assert plain_http.status_code == 422
    nothing = await rig.client.post(
        f"{V1}/identity-providers", json={"protocol": "saml", "name": "bare"}, headers=rig.admin
    )
    assert {e["path"] for e in nothing.json()["errors"]} == {
        "idp_entity_id",
        "sso_url",
        "signing_certificates",
    }


async def test_signed_authn_requests_and_the_metadata_that_announces_them(rig: Rig) -> None:
    keys = make_idp()
    made = (
        await rig.client.post(
            f"{V1}/identity-providers",
            json={
                "protocol": "saml",
                "name": "corp",
                "idp_metadata_xml": idp_metadata(keys),
                "sign_requests": True,
            },
            headers=rig.admin,
        )
    ).json()
    assert made["sign_requests"] is True and made["sp_signing_certificate"]
    assert made["sp_signing_key_ref"] == "kv/idp/corp/sp-signing-key"
    key_pem = await rig.secrets.get(made["sp_signing_key_ref"])
    assert "PRIVATE KEY" in key_pem and "PRIVATE KEY" not in str(made)

    metadata = (await rig.client.get(f"{V1}/auth/saml/{made['id']}/metadata")).text
    root = etree.fromstring(metadata.encode())
    ns = {"md": "urn:oasis:names:tc:SAML:2.0:metadata", "ds": "http://www.w3.org/2000/09/xmldsig#"}
    descriptor = root.find("md:SPSSODescriptor", ns)
    assert descriptor is not None and descriptor.get("AuthnRequestsSigned") == "true"
    assert root.find(".//md:KeyDescriptor/ds:KeyInfo//ds:X509Certificate", ns) is not None

    started = await rig.client.get(f"{V1}/auth/login/{made['id']}")
    assert started.status_code == 302
    location = started.headers["location"]
    query = parse_qs(urlparse(location).query)
    assert query["SigAlg"] == ["http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"]
    signed = urlencode(
        {
            "SAMLRequest": query["SAMLRequest"][0],
            "RelayState": query["RelayState"][0],
            "SigAlg": query["SigAlg"][0],
        }
    )
    cert = x509.load_pem_x509_certificate(made["sp_signing_certificate"].encode())
    public_key = cert.public_key()
    assert isinstance(public_key, rsa.RSAPublicKey)
    public_key.verify(
        base64.b64decode(query["Signature"][0]),
        signed.encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    serialization.load_pem_private_key(key_pem.encode(), password=None)  # the stored key is usable


# -- test sign-in -----------------------------------------------------------------------------


async def start_test(rig: Rig, provider: dict[str, Any]) -> tuple[str, str]:
    started = await rig.client.post(
        f"{V1}/identity-providers/{provider['id']}/test",
        json={"return_to": "/admin/identity-providers"},
        headers=rig.admin,
    )
    assert started.status_code == 201, started.text
    return started.json()["test_id"], started.json()["authorize_url"]


async def test_oidc_test_signin_returns_claims_and_would_grant_but_creates_no_session(
    rig: Rig,
) -> None:
    provider = await rig.create_oidc()
    await rig.client.post(
        f"{V1}/role-mappings",
        json={
            "provider": "authentik",
            "group_name": "acme-ops",
            "role": "operator",
            "tenant_id": str(TENANT),
        },
        headers=rig.admin,
    )
    test_id, authorize = await start_test(rig, provider)
    url = f"{V1}/identity-providers/{provider['id']}/test/{test_id}"
    pending = await rig.client.get(url, headers=rig.admin)
    assert pending.json() == {
        "status": "pending",
        "subject": None,
        "username": None,
        "email": None,
        "groups": None,
        "claims": None,
        "would_grant": None,
        "warnings": None,
        "error": None,
    }

    code = rig.oidc.authorize(authorize, groups=["acme-ops", "unmapped"])
    state = parse_qs(urlparse(authorize).query)["state"][0]
    callback = await rig.client.get(
        f"{V1}/auth/callback/{provider['id']}", params={"code": code, "state": state}
    )
    assert callback.status_code == 302
    assert callback.headers["location"] == "/admin/identity-providers"
    assert "set-cookie" not in callback.headers, "a test sign-in sets no cookie"

    done = await rig.client.get(url, headers=rig.admin)
    body = done.json()
    assert body["status"] == "done" and body["error"] is None
    assert (body["subject"], body["username"], body["email"]) == (
        "u-1",
        "alice",
        "a@example.test",
    )
    assert body["groups"] == ["acme-ops", "unmapped"]
    assert body["would_grant"] == [
        {
            "tenant": "acme",
            "role": "operator",
            "via": {"provider": "authentik", "group": "acme-ops"},
        }
    ]
    assert body["claims"]["sub"] == "u-1" and "nonce" not in body["claims"]
    for secret in ("s3cret-value", "access_token", "id_token"):
        assert secret not in done.text
    assert (await rig.client.get(url, headers=rig.admin)).status_code == 404, "read once"
    assert await rig.sql("SELECT count(*) FROM user_session") == 0
    assert await rig.sql("SELECT count(*) FROM user_account") == 0
    assert await rig.sql("SELECT count(*) FROM audit_event WHERE action='idp.test_done'") == 1


async def test_test_signin_reports_warnings_failures_and_expires(rig: Rig) -> None:
    provider = await rig.create_oidc()
    disabled = await rig.client.patch(
        f"{V1}/identity-providers/{provider['id']}",
        json={"protocol": "oidc", "enabled": False},
        headers=rig.admin | {"If-Match": '"1"'},
    )
    assert disabled.status_code == 200

    # no mapping matches and the e-mail is missing; a disabled provider can still be tested
    test_id, authorize = await start_test(rig, provider)
    code = rig.oidc.authorize(authorize, groups=["nobody"])
    state = parse_qs(urlparse(authorize).query)["state"][0]
    await rig.client.get(
        f"{V1}/auth/callback/{provider['id']}", params={"code": code, "state": state}
    )
    url = f"{V1}/identity-providers/{provider['id']}/test/{test_id}"
    body = (await rig.client.get(url, headers=rig.admin)).json()
    assert body["would_grant"] == []
    assert any("no role mapping matches" in w for w in body["warnings"])

    # the provider refuses the code: the failure is the result, not an HTTP error
    test_id, authorize = await start_test(rig, provider)
    rig.oidc.refuse_code = True
    code = rig.oidc.authorize(authorize, groups=[])
    state = parse_qs(urlparse(authorize).query)["state"][0]
    await rig.client.get(
        f"{V1}/auth/callback/{provider['id']}", params={"code": code, "state": state}
    )
    failed = (
        await rig.client.get(
            f"{V1}/identity-providers/{provider['id']}/test/{test_id}", headers=rig.admin
        )
    ).json()
    assert failed["status"] == "done" and "refused" in failed["error"]

    # nobody finished this one: five minutes later it is gone
    test_id, _ = await start_test(rig, provider)
    rig.clock.now += timedelta(minutes=6)
    gone = await rig.client.get(
        f"{V1}/identity-providers/{provider['id']}/test/{test_id}", headers=rig.admin
    )
    assert gone.status_code == 404
    unknown = await rig.client.get(
        f"{V1}/identity-providers/{provider['id']}/test/{provider['id']}", headers=rig.admin
    )
    assert unknown.status_code == 404


async def test_saml_test_signin(rig: Rig) -> None:
    keys = make_idp()
    provider = (
        await rig.client.post(
            f"{V1}/identity-providers",
            json={"protocol": "saml", "name": "corp", "idp_metadata_xml": idp_metadata(keys)},
            headers=rig.admin,
        )
    ).json()
    await rig.client.post(
        f"{V1}/role-mappings",
        json={
            "provider": "corp",
            "group_name": "sy-admins",
            "role": "tenant-admin",
            "tenant_id": str(TENANT),
        },
        headers=rig.admin,
    )
    test_id, authorize = await start_test(rig, provider)
    query = parse_qs(urlparse(authorize).query)
    request_id = etree.fromstring(
        zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15)
    ).get("ID")
    acs = f"{BASE}{V1}/auth/callback/{provider['id']}"
    xml = response_xml(
        request_id or "",
        audience=provider["sp_entity_id"],
        recipient=acs,
        destination=acs,
        issuer=IDP_ENTITY,
        now=rig.clock.now,
    )
    posted = await rig.client.post(
        f"{V1}/auth/callback/{provider['id']}",
        content=urlencode(
            {"SAMLResponse": encode(sign(xml, keys)), "RelayState": query["RelayState"][0]}
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert posted.status_code == 302 and "set-cookie" not in posted.headers
    body = (
        await rig.client.get(
            f"{V1}/identity-providers/{provider['id']}/test/{test_id}", headers=rig.admin
        )
    ).json()
    assert body["status"] == "done" and body["subject"] == "alice-id"
    assert body["would_grant"][0]["role"] == "tenant-admin"
    assert await rig.sql("SELECT count(*) FROM user_session") == 0


async def test_deleting_a_provider_removes_its_mappings_users_and_sessions(rig: Rig) -> None:
    provider = await rig.create_oidc()
    await rig.client.post(
        f"{V1}/role-mappings",
        json={
            "provider": "authentik",
            "group_name": "g",
            "role": "viewer",
            "tenant_id": str(TENANT),
        },
        headers=rig.admin,
    )
    started = await rig.client.get(f"{V1}/auth/login/{provider['id']}")
    code = rig.oidc.authorize(started.headers["location"], groups=["g"])
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    await rig.client.get(
        f"{V1}/auth/callback/{provider['id']}", params={"code": code, "state": state}
    )
    assert await rig.sql("SELECT count(*) FROM user_session") == 1
    await rig.client.delete(
        f"{V1}/identity-providers/{provider['id']}", headers=rig.admin | {"If-Match": '"1"'}
    )
    for table in ("idp_role_mapping", "user_account", "user_session"):
        assert await rig.sql(f"SELECT count(*) FROM {table}") == 0, table


# -- break-glass ------------------------------------------------------------------------------


async def test_break_glass_enrolment_confirmation_and_timed_enablement(rig: Rig) -> None:
    status = (await rig.client.get(f"{V1}/system/break-glass", headers=rig.admin)).json()
    assert status["forced_by_deployment"] is False and status["enabled"] is False
    assert status["admin"] is None

    too_early = await rig.client.put(
        f"{V1}/system/break-glass",
        json={"enabled": True, "reason": "test"},
        headers=rig.admin,
    )
    assert too_early.status_code == 409, "needs a confirmed administrator"

    enrol = await rig.client.post(f"{V1}/system/break-glass/admin", headers=rig.admin)
    assert enrol.status_code == 201, enrol.text
    shown = enrol.json()
    assert shown["otpauth_uri"].startswith("otpauth://totp/Switchyard:breakglass?secret=")
    assert shown["totp_secret"] in shown["otpauth_uri"] and len(shown["password"]) >= 20
    assert await rig.secrets.get("kv/break-glass/breakglass-totp") == shown["totp_secret"]
    listed = (await rig.client.get(f"{V1}/system/break-glass", headers=rig.admin)).json()
    assert listed["admin"]["username"] == "breakglass" and not listed["admin"]["totp_confirmed"]
    assert shown["password"] not in str(listed) and shown["totp_secret"] not in str(listed)

    # logging in needs the switch on, and an unconfirmed administrator cannot log in at all
    login = {
        "username": "breakglass",
        "password": shown["password"],
        "totp": totp_code(shown["totp_secret"], rig.clock.now.timestamp()),
    }
    assert (await rig.client.post(f"{V1}/auth/break-glass", json=login)).status_code == 401

    wrong = await rig.client.post(
        f"{V1}/system/break-glass/admin/confirm", json={"totp": "000000"}, headers=rig.admin
    )
    assert wrong.status_code == 422 and wrong.json()["errors"][0]["path"] == "totp"
    confirmed = await rig.client.post(
        f"{V1}/system/break-glass/admin/confirm",
        json={"totp": login["totp"]},
        headers=rig.admin,
    )
    assert confirmed.status_code == 200 and confirmed.json()["admin"]["totp_confirmed"] is True

    no_reason = await rig.client.put(
        f"{V1}/system/break-glass", json={"enabled": True}, headers=rig.admin
    )
    assert no_reason.status_code == 422 and no_reason.json()["errors"][0]["path"] == "reason"
    too_long = await rig.client.put(
        f"{V1}/system/break-glass",
        json={"enabled": True, "reason": "x", "ttl_minutes": 5000},
        headers=rig.admin,
    )
    assert too_long.status_code == 422

    providers = (await rig.client.get(f"{V1}/auth/providers")).json()
    assert providers["break_glass"] is False
    on = await rig.client.put(
        f"{V1}/system/break-glass",
        json={"enabled": True, "reason": "SSO outage", "ttl_minutes": 30},
        headers=rig.admin,
    )
    assert on.status_code == 200 and on.json()["enabled"] is True
    assert on.json()["reason"] == "SSO outage"
    expected_until = (rig.clock.now + timedelta(minutes=30)).replace(tzinfo=UTC)
    assert datetime.fromisoformat(on.json()["enabled_until"]) == expected_until
    assert (await rig.client.get(f"{V1}/auth/providers")).json()["break_glass"] is True

    login["totp"] = totp_code(shown["totp_secret"], rig.clock.now.timestamp())
    ok = await rig.client.post(f"{V1}/auth/break-glass", json=login)
    assert ok.status_code == 200 and ok.json()["break_glass"] is True
    assert ok.json()["session"]["idle_timeout_seconds"] == 900
    assert datetime.fromisoformat(ok.json()["session"]["expires_at"]) <= rig.clock.now + timedelta(
        hours=1, seconds=5
    )
    rig.client.cookies.clear()

    rig.clock.now += timedelta(minutes=31)
    assert (await rig.client.get(f"{V1}/auth/providers")).json()["break_glass"] is False
    login["totp"] = totp_code(shown["totp_secret"], rig.clock.now.timestamp())
    assert (await rig.client.post(f"{V1}/auth/break-glass", json=login)).status_code == 401
    expired = (await rig.client.get(f"{V1}/system/break-glass", headers=rig.admin)).json()
    assert expired["enabled"] is False and expired["enabled_until"] is None

    off = await rig.client.put(
        f"{V1}/system/break-glass", json={"enabled": False}, headers=rig.admin
    )
    assert off.status_code == 200
    assert (
        await rig.client.delete(f"{V1}/system/break-glass/admin", headers=rig.admin)
    ).status_code == 204
    assert (
        await rig.client.delete(f"{V1}/system/break-glass/admin", headers=rig.admin)
    ).status_code == 404
    actions = {
        "break_glass.admin_created",
        "break_glass.admin_confirmed",
        "break_glass.confirm_failed",
        "break_glass.enabled",
        "break_glass.disabled",
        "break_glass.admin_deleted",
        "auth.break_glass_login",
    }
    for action in actions:
        assert await rig.sql(f"SELECT count(*) FROM audit_event WHERE action='{action}'") >= 1, (
            action
        )
    leaked = await rig.sql(
        f"SELECT count(*) FROM audit_event WHERE diff::text LIKE '%{shown['password']}%' "
        f"OR diff::text LIKE '%{shown['totp_secret']}%'"
    )
    assert leaked == 0, "neither the password nor the TOTP secret reaches the audit log"


async def test_a_deployment_forced_break_glass_cannot_be_turned_off_by_the_api(rig: Rig) -> None:
    sessions = SessionService(
        rig.w.db,
        OidcClient(transport=httpx.MockTransport(rig.oidc.handler)),
        rig.secrets,
        break_glass_enabled=True,
        clock=rig.clock,
    )
    services = build_services(rig.w.db, StaticEngineDirectory({}), rig.secrets, sessions=sessions)
    app = create_app(services, public_url=BASE)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        status = (await client.get(f"{V1}/system/break-glass", headers=rig.admin)).json()
        assert status["forced_by_deployment"] is True and status["enabled"] is True
        off = await client.put(
            f"{V1}/system/break-glass", json={"enabled": False}, headers=rig.admin
        )
        assert off.json()["forced_by_deployment"] is True and off.json()["enabled"] is True
        assert (await client.get(f"{V1}/auth/providers")).json()["break_glass"] is True


async def test_break_glass_administration_is_for_system_administrators(rig: Rig) -> None:
    plaintext, _ = await rig.services.tokens.create(ROOT, TENANT, "ta", Role.TENANT_ADMIN)
    headers = {"Authorization": f"Bearer {plaintext}"}
    for method, path in [
        ("GET", "/system/break-glass"),
        ("PUT", "/system/break-glass"),
        ("POST", "/system/break-glass/admin"),
        ("DELETE", "/system/break-glass/admin"),
    ]:
        response = await rig.client.request(
            method, V1 + path, headers=headers, json={"enabled": False} if method == "PUT" else None
        )
        assert response.status_code == 403, path
        assert response.json()["required_role"] == "system-admin"


# -- system tokens ----------------------------------------------------------------------------


async def test_system_tokens_are_a_separate_resource(rig: Rig) -> None:
    # a token cannot create tokens, so a signed-in person does it (service call as that person)
    plaintext, record = await rig.services.tokens.create_system(
        ROOT, "bootstrap", ttl=timedelta(days=30)
    )
    system = {"Authorization": f"Bearer {plaintext}"}
    listed = (await rig.client.get(f"{V1}/system/tokens", headers=rig.admin)).json()
    assert {t["name"] for t in listed} >= {"bootstrap", "sys"}
    entry = next(t for t in listed if t["name"] == "bootstrap")
    assert entry["role"] == "system-admin" and entry["revoked"] is False
    assert "token" not in entry and plaintext not in str(listed)
    tenant_listing = (await rig.client.get(f"{V1}/tenants/acme/tokens", headers=rig.admin)).json()
    assert tenant_listing == [], "system tokens are not tenant tokens"

    by_token = await rig.client.post(f"{V1}/system/tokens", json={"name": "child"}, headers=system)
    assert by_token.status_code == 403 and "cannot create tokens" in by_token.json()["detail"]
    too_long = await rig.client.post(
        f"{V1}/system/tokens", json={"name": "x", "ttl_days": 91}, headers=rig.admin
    )
    assert too_long.status_code == 422
    tenant_route = await rig.client.post(
        f"{V1}/tenants/acme/tokens", json={"name": "x", "role": "system-admin"}, headers=rig.admin
    )
    assert tenant_route.status_code == 403  # a token may not create tokens at all
    assert await rig.sql("SELECT count(*) FROM audit_event WHERE action='token.system_used'") >= 1

    plaintext2, _ = await rig.services.tokens.create(ROOT, TENANT, "ta", Role.TENANT_ADMIN)
    denied = await rig.client.get(
        f"{V1}/system/tokens", headers={"Authorization": f"Bearer {plaintext2}"}
    )
    assert denied.status_code == 403 and denied.json()["required_role"] == "system-admin"

    revoked = await rig.client.delete(f"{V1}/system/tokens/{record.id}", headers=rig.admin)
    assert revoked.status_code == 204
    assert (await rig.client.get(f"{V1}/me", headers=system)).status_code == 401
    assert (
        await rig.client.delete(f"{V1}/system/tokens/{record.id}", headers=rig.admin)
    ).status_code == 404

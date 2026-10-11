"""Browser login: OIDC with PKCE against a fake provider, sessions, CSRF, per-tenant roles."""

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy import text

from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.idp import IdentityProviderAdmin
from switchyard.services.oidc import OidcClient
from switchyard.services.sessions import (
    SessionService,
    hash_password,
    safe_return_to,
    totp_code,
    verify_password,
)
from switchyard.services.tenants import TenantService
from tests.pipeline_env import GROUP, TENANT, World, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")

ISSUER = "https://idp.example.test"
CLIENT = "switchyard"
V1 = "/api/v1"


@dataclass
class FakeIdp:
    """An OpenID Connect provider that signs real ID tokens and checks PKCE."""

    key: RSAKey = field(default_factory=lambda: RSAKey.generate_key(2048, {"kid": "k1"}))
    claims: dict[str, Any] = field(default_factory=dict)
    pending: dict[str, dict[str, str]] = field(default_factory=dict)  # code -> request
    wrong_nonce: bool = False
    refuse_code: bool = False

    def authorize(self, url: str, **claims: Any) -> str:
        """Play the user agreeing at the provider; returns the code the redirect would carry."""
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert query["code_challenge_method"] == "S256" and query["client_id"] == CLIENT
        code = f"code-{len(self.pending)}"
        self.pending[code] = query
        self.claims = {
            "sub": "u-1",
            "preferred_username": "alice",
            "email": "a@example.test",
        } | claims
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json=KeySet([self.key]).as_dict(private=False))
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            original = self.pending.pop(form["code"], None)
            if original is None or self.refuse_code:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            assert challenge == original["code_challenge"], "PKCE verifier must match"
            assert request.headers["authorization"].startswith("Basic ")
            now = int(datetime.now(UTC).timestamp())
            claims = {
                "iss": ISSUER,
                "aud": CLIENT,
                "iat": now,
                "exp": now + 300,
                "nonce": "wrong" if self.wrong_nonce else original["nonce"],
            } | self.claims
            token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, self.key)
            return httpx.Response(200, json={"id_token": token, "access_token": "unused"})
        return httpx.Response(404)


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Rig:
    w: World
    idp: FakeIdp
    clock: Clock
    secrets: MemorySecrets
    services: ApiServices
    client: httpx.AsyncClient
    globex: UUID

    async def login(
        self, *, groups: list[str], return_to: str = "/t/acme", **claims: Any
    ) -> httpx.Response:
        started = await self.client.get(
            f"{V1}/auth/login/authentik", params={"return_to": return_to}
        )
        assert started.status_code == 302, started.text
        code = self.idp.authorize(started.headers["location"], groups=groups, **claims)
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        return await self.client.get(
            f"{V1}/auth/callback/authentik", params={"code": code, "state": state}
        )


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        clock, idp = Clock(), FakeIdp()
        secrets = MemorySecrets(
            {"kv/idp/authentik": "s3cret", "kv/break-glass/totp": "JBSWY3DPEHPK3PXP"}
        )
        oidc = OidcClient(transport=httpx.MockTransport(idp.handler))
        sessions = SessionService(w.db, oidc, secrets, clock=clock)
        globex = (
            await TenantService(w.db).create(principal(Role.SYSTEM_ADMIN, "root"), "globex")
        ).id
        admin = IdentityProviderAdmin(w.db)
        await admin.add_provider("authentik", ISSUER, CLIENT, client_secret_ref="kv/idp/authentik")
        await admin.map_group("authentik", "acme-admins", Role.TENANT_ADMIN, tenant="acme")
        await admin.map_group("authentik", "globex-viewers", Role.VIEWER, tenant="globex")
        await admin.map_group("authentik", "platform", Role.SYSTEM_ADMIN)
        services = build_services(w.db, StaticEngineDirectory({}), sessions=sessions)
        app = create_app(services, public_url="https://switchyard.example.test")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://switchyard.example.test"
        ) as client:
            yield Rig(w, idp, clock, secrets, services, client, globex)


async def count(rig: Rig, sql: str) -> int:
    async with admin_session(rig.w.db.admin) as s:
        return int((await s.execute(text(sql))).scalar_one())


async def test_login_round_trip_gives_a_hardened_cookie_and_per_tenant_roles(rig: Rig) -> None:
    providers = (await rig.client.get(f"{V1}/auth/providers")).json()
    assert [p["name"] for p in providers["providers"]] == ["authentik"]
    assert providers["break_glass"] is False
    done = await rig.login(groups=["acme-admins", "globex-viewers"])
    assert done.status_code == 302 and done.headers["location"] == "/t/acme"
    cookie = done.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    me = (await rig.client.get(f"{V1}/me")).json()
    assert me["name"] == "alice" and me["auth"] == "session" and not me["is_system"]
    roles = {t["name"]: t["role"] for t in me["tenants"]}
    assert roles == {"acme": "tenant-admin", "globex": "viewer"}
    assert me["csrf_token"]
    assert me["principal"] == "user:authentik:u-1"
    assert await count(rig, "SELECT count(*) FROM audit_event WHERE action='auth.login'") == 1


async def test_roles_are_enforced_per_tenant_and_writes_need_the_csrf_header(rig: Rig) -> None:
    await rig.login(groups=["acme-admins", "globex-viewers"])
    csrf = (await rig.client.get(f"{V1}/me")).json()["csrf_token"]
    token = {"X-CSRF-Token": csrf}
    body = {"name": "se-a", "dataplane_url": "https://se-a.invalid:5556"}
    group = f"{V1}/tenants/acme/se-groups/{GROUP}/engines"
    no_csrf = await rig.client.post(group, json=body)
    assert no_csrf.status_code == 403 and "CSRF" in no_csrf.text
    wrong = await rig.client.post(group, json=body, headers={"X-CSRF-Token": "nope"})
    assert wrong.status_code == 403
    ok = await rig.client.post(group, json=body, headers=token)
    assert ok.status_code == 201, ok.text
    other = await rig.client.post(
        f"{V1}/tenants/globex/se-groups/{GROUP}/engines", json=body, headers=token
    )
    assert other.status_code == 403, "viewer in globex must not write there"
    reads = await rig.client.get(f"{V1}/tenants/globex/virtual-services")
    assert reads.status_code == 200
    assert (await rig.client.get(f"{V1}/tenants")).status_code == 200


async def test_system_admin_group_and_bearer_tokens_keep_working(rig: Rig) -> None:
    await rig.login(groups=["platform"])
    me = (await rig.client.get(f"{V1}/me")).json()
    assert me["is_system"] and {t["name"] for t in me["tenants"]} >= {"acme", "globex"}
    rig.client.cookies.clear()
    token, _ = await rig.services.tokens.create(
        principal(Role.SYSTEM_ADMIN, "root"), TENANT, "ci", Role.VIEWER
    )
    bearer = (await rig.client.get(f"{V1}/me", headers={"Authorization": f"Bearer {token}"})).json()
    assert bearer["auth"] == "token" and bearer["csrf_token"] is None
    assert bearer["principal"], "tokens get a principal too"
    assert bearer["tenants"][0]["role"] == "viewer"


async def test_expired_idle_and_logged_out_sessions_are_401_problems(rig: Rig) -> None:
    await rig.login(groups=["acme-admins"])
    csrf = (await rig.client.get(f"{V1}/me")).json()["csrf_token"]
    rig.clock.now += timedelta(minutes=31)
    idle = await rig.client.get(f"{V1}/me")
    assert idle.status_code == 401 and idle.json()["code"] == "session_expired"
    assert idle.headers["content-type"].startswith("application/problem+json")

    await rig.login(groups=["acme-admins"])
    csrf = (await rig.client.get(f"{V1}/me")).json()["csrf_token"]
    for _ in range(10):  # stay active, but not forever
        rig.clock.now += timedelta(minutes=25)
        assert (await rig.client.get(f"{V1}/me")).status_code in {200, 401}
    assert (await rig.client.get(f"{V1}/me")).status_code == 401, "absolute lifetime is 4 hours"

    await rig.login(groups=["acme-admins"])
    csrf = (await rig.client.get(f"{V1}/me")).json()["csrf_token"]
    assert (
        await rig.client.post(f"{V1}/auth/logout", headers={"X-CSRF-Token": csrf})
    ).status_code == 204
    again = await rig.client.get(f"{V1}/me")
    assert again.status_code == 401
    assert await count(rig, "SELECT count(*) FROM audit_event WHERE action='auth.logout'") == 1


async def test_failed_logins_are_refused_and_audited(rig: Rig) -> None:
    denied = await rig.login(groups=["nobody"])
    assert denied.status_code == 302 and denied.headers["location"] == "/login?error=no_access"
    assert (
        await count(rig, "SELECT count(*) FROM audit_event WHERE action='auth.login_denied'") == 1
    )

    rig.idp.wrong_nonce = True
    bad = await rig.login(groups=["acme-admins"])
    assert bad.headers["location"] == "/login?error=provider_error"
    rig.idp.wrong_nonce = False

    rig.idp.refuse_code = True
    refused = await rig.login(groups=["acme-admins"])
    assert refused.headers["location"] == "/login?error=provider_error"
    rig.idp.refuse_code = False
    assert (
        await count(rig, "SELECT count(*) FROM audit_event WHERE action='auth.login_failed'") == 2
    )

    unknown_state = await rig.client.get(
        f"{V1}/auth/callback/authentik", params={"code": "x", "state": "never-issued"}
    )
    assert unknown_state.headers["location"] == "/login?error=expired"
    assert (await rig.client.get(f"{V1}/auth/login/nope")).status_code == 400
    assert "set-cookie" not in unknown_state.headers


async def test_a_login_state_works_once(rig: Rig) -> None:
    started = await rig.client.get(f"{V1}/auth/login/authentik")
    code = rig.idp.authorize(started.headers["location"], groups=["acme-admins"])
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    params = {"code": code, "state": state}
    assert (await rig.client.get(f"{V1}/auth/callback/authentik", params=params)).status_code == 302
    replay = await rig.client.get(f"{V1}/auth/callback/authentik", params=params)
    assert replay.headers["location"] == "/login?error=expired"


async def test_break_glass_is_off_by_default_then_works_with_totp_and_locks_out(
    rig: Rig, pg_url: str
) -> None:
    body = {"username": "root", "password": "x" * 16, "totp": "000000"}
    off = await rig.client.post(f"{V1}/auth/break-glass", json=body)
    assert off.status_code == 401 and "not enabled" in off.text

    sessions = SessionService(
        rig.w.db,
        OidcClient(transport=httpx.MockTransport(rig.idp.handler)),
        rig.secrets,
        break_glass_enabled=True,
        clock=rig.clock,
    )
    await sessions.create_local_admin("root", "correct horse battery", "kv/break-glass/totp")
    rig.services.sessions = sessions
    code = totp_code("JBSWY3DPEHPK3PXP", rig.clock.now.timestamp())
    good = {"username": "root", "password": "correct horse battery", "totp": code}
    wrong_pw = await rig.client.post(
        f"{V1}/auth/break-glass", json=good | {"password": "wrong" * 5}
    )
    assert wrong_pw.status_code == 401
    wrong_code = await rig.client.post(f"{V1}/auth/break-glass", json=good | {"totp": "123456"})
    assert wrong_code.status_code == 401
    ok = await rig.client.post(f"{V1}/auth/break-glass", json=good)
    assert ok.status_code == 200, ok.text
    assert ok.json()["is_system"] and ok.json()["break_glass"]
    me = (await rig.client.get(f"{V1}/me")).json()
    assert me["name"] == "root" and me["principal"] == "user:local:root"
    assert (
        await count(rig, "SELECT count(*) FROM audit_event WHERE action='auth.break_glass_login'")
        == 1
    )

    for _ in range(5):
        await rig.client.post(f"{V1}/auth/break-glass", json=good | {"password": "nope" * 5})
    locked = await rig.client.post(f"{V1}/auth/break-glass", json=good)
    assert locked.status_code == 401 and "too many" in locked.text
    rig.clock.now += timedelta(minutes=16)
    assert (await rig.client.post(f"{V1}/auth/break-glass", json=good)).status_code == 401, (
        "the TOTP code of the earlier time step no longer matches"
    )
    fresh = totp_code("JBSWY3DPEHPK3PXP", rig.clock.now.timestamp())
    assert (
        await rig.client.post(f"{V1}/auth/break-glass", json=good | {"totp": fresh})
    ).status_code == 200
    assert (
        await count(rig, "SELECT count(*) FROM audit_event WHERE action='auth.break_glass_failed'")
        >= 7
    )


async def test_login_is_unavailable_when_not_configured(pg_url: str) -> None:
    async with world(pg_url) as w:
        app = create_app(build_services(w.db, StaticEngineDirectory({})))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as client:
            assert (await client.get(f"{V1}/auth/providers")).status_code == 503
            assert (await client.get(f"{V1}/me")).status_code == 401


def test_return_to_cannot_be_an_open_redirect() -> None:
    assert safe_return_to("/t/acme?x=1") == "/t/acme?x=1"
    for bad in ("https://evil.example", "//evil.example", "/\\evil", "", None, "javascript:1"):
        assert safe_return_to(bad) == "/"


def test_password_hashes_verify_and_are_salted() -> None:
    a, b = hash_password("a long passphrase", n=2**10), hash_password("a long passphrase", n=2**10)
    assert a != b and verify_password("a long passphrase", a)
    assert not verify_password("another passphrase", a) and not verify_password("x", "garbage")


def test_totp_matches_the_rfc_6238_vector() -> None:
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_code(secret, 59, digits=8) == "94287082"
    assert json.dumps(secret)

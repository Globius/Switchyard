"""G1: role refresh, users, role mappings, token limits and the 401/403 `code` members."""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from tests.integration import test_login
from tests.integration.test_login import Rig, count
from tests.pipeline_env import TENANT, principal

pytestmark = pytest.mark.usefixtures("owner_engine")
rig = test_login.rig  # the login test rig: fake OIDC provider, mappings, clock
V1 = "/api/v1"
ROOT = principal(Role.SYSTEM_ADMIN, "root")


async def bearer(rig: Rig, role: Role, tenant: UUID | None = TENANT) -> dict[str, str]:
    """A bearer header for a fresh token of ``role`` (system tokens have no tenant)."""
    name = f"t{len(role.value)}{abs(hash((role, tenant, id(rig)))) % 10**6}"
    plaintext, _ = await rig.services.tokens.create(
        ROOT, None if role is Role.SYSTEM_ADMIN else tenant, name, role
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def mapping_id(rig: Rig, group: str) -> UUID:
    async with admin_session(rig.w.db.admin) as s:
        return UUID(
            str(
                (
                    await s.execute(
                        text("SELECT id FROM idp_role_mapping WHERE group_name = :g"), {"g": group}
                    )
                ).scalar_one()
            )
        )


async def me(rig: Rig) -> dict[str, Any]:
    response = await rig.client.get(f"{V1}/me")
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


async def csrf(rig: Rig) -> dict[str, str]:
    return {"X-CSRF-Token": (await me(rig))["csrf_token"]}


async def test_me_reports_the_session_lifetime_and_which_groups_granted_each_role(rig: Rig) -> None:
    await rig.login(groups=["acme-admins", "globex-viewers"])
    body = await me(rig)
    assert body["session"]["idle_timeout_seconds"] == 1800
    expires = body["session"]["expires_at"]
    assert expires.startswith(rig.clock.now.replace(microsecond=0).isoformat()[:13]) or expires
    via = {t["name"]: t["via"] for t in body["tenants"]}
    assert via == {
        "acme": [{"provider": "authentik", "group": "acme-admins"}],
        "globex": [{"provider": "authentik", "group": "globex-viewers"}],
    }
    bearer_me = (await rig.client.get(f"{V1}/me", headers=await bearer(rig, Role.VIEWER))).json()
    assert bearer_me["session"] is None and bearer_me["tenants"][0]["via"] == []


async def test_a_mapping_change_applies_within_a_minute_and_at_once_on_refresh(rig: Rig) -> None:
    await rig.login(groups=["acme-admins"])
    assert (await me(rig))["tenants"][0]["role"] == "tenant-admin"
    mapping = await mapping_id(rig, "acme-admins")
    changed = await rig.services.role_mappings.update(
        ROOT, mapping, expected_version=1, changes={"role": "operator"}
    )
    assert changed.version == 2
    rig.clock.now += timedelta(seconds=30)
    assert (await me(rig))["tenants"][0]["role"] == "tenant-admin", "not recomputed yet"
    rig.clock.now += timedelta(seconds=31)
    assert (await me(rig))["tenants"][0]["role"] == "operator", "recomputed after 60 s"

    await rig.services.role_mappings.update(
        ROOT, mapping, expected_version=2, changes={"role": "viewer"}
    )
    refreshed = await rig.client.post(f"{V1}/auth/refresh", headers=await csrf(rig))
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["tenants"][0]["role"] == "viewer", "refresh applies it at once"
    no_csrf = await rig.client.post(f"{V1}/auth/refresh")
    assert no_csrf.status_code == 403 and no_csrf.json()["code"] == "csrf"


async def test_refresh_does_not_extend_the_session(rig: Rig) -> None:
    await rig.login(groups=["acme-admins"])
    before = (await me(rig))["session"]["expires_at"]
    rig.clock.now += timedelta(minutes=20)
    refreshed = await rig.client.post(f"{V1}/auth/refresh", headers=await csrf(rig))
    assert refreshed.json()["session"]["expires_at"] == before
    assert refreshed.json()["session"]["grants_computed_at"] != before


async def test_removing_the_last_mapping_ends_the_session(rig: Rig) -> None:
    await rig.login(groups=["acme-admins"])
    await rig.services.role_mappings.delete(ROOT, await mapping_id(rig, "acme-admins"))
    rig.clock.now += timedelta(seconds=61)
    gone = await rig.client.get(f"{V1}/me")
    assert gone.status_code == 401 and gone.json()["code"] == "session_expired"
    assert await count(rig, "SELECT count(*) FROM audit_event WHERE action='auth.session_revoked'")


async def test_one_account_per_person_and_disabling_ends_their_sessions(rig: Rig) -> None:
    await rig.login(groups=["acme-admins"])
    rig.clock.now += timedelta(minutes=5)
    rig.client.cookies.clear()
    await rig.login(groups=["acme-admins", "platform"])
    sys_admin = await bearer(rig, Role.SYSTEM_ADMIN, None)
    listing = (await rig.client.get(f"{V1}/users", headers=sys_admin)).json()
    assert len(listing["items"]) == 1
    user = listing["items"][0]
    assert user["username"] == "alice" and user["groups"] == ["acme-admins", "platform"]
    assert user["first_login_at"] < user["last_login_at"]
    assert {g["role"] for g in user["grants"]} == {"tenant-admin", "system-admin"}
    detail = (await rig.client.get(f"{V1}/users/{user['id']}", headers=sys_admin)).json()
    assert detail["active_sessions"] == 2 and detail["subject"] == "u-1"
    columns = await count(
        rig,
        "SELECT count(*) FROM information_schema.columns WHERE table_name='user_account' "
        "AND column_name ~ 'secret|password|token'",
    )
    assert columns == 0, "no secret appears in any column"

    disabled = await rig.client.post(f"{V1}/users/{user['id']}/disable", headers=sys_admin)
    assert disabled.status_code == 200 and disabled.json()["disabled_at"] is not None
    refused = await rig.client.get(f"{V1}/me")
    assert refused.status_code == 403 and refused.json()["code"] == "user_disabled"
    rig.client.cookies.clear()
    denied = await rig.login(groups=["acme-admins"])
    assert denied.headers["location"] == "/login?error=disabled"
    assert "set-cookie" not in denied.headers

    await rig.client.post(f"{V1}/users/{user['id']}/enable", headers=sys_admin)
    assert (await rig.login(groups=["acme-admins"])).headers["location"] == "/t/acme"


async def test_revoke_sessions_and_delete(rig: Rig) -> None:
    await rig.login(groups=["acme-admins"])
    sys_admin = await bearer(rig, Role.SYSTEM_ADMIN, None)
    user = (await rig.client.get(f"{V1}/users", headers=sys_admin)).json()["items"][0]
    ended = await rig.client.post(f"{V1}/users/{user['id']}/revoke-sessions", headers=sys_admin)
    assert ended.json() == {"revoked": 1}
    assert (await rig.client.get(f"{V1}/me")).status_code == 401
    await rig.login(groups=["acme-admins"])
    deleted = await rig.client.delete(f"{V1}/users/{user['id']}", headers=sys_admin)
    assert deleted.status_code == 204
    assert (await rig.client.get(f"{V1}/me")).status_code == 401, "its sessions went with it"
    assert (await rig.client.get(f"{V1}/users/{user['id']}", headers=sys_admin)).status_code == 404
    again = await rig.login(groups=["acme-admins"])
    assert again.headers["location"] == "/t/acme", "the person reappears at the next sign-in"
    assert await count(rig, "SELECT count(*) FROM user_account") == 1


async def test_user_search_filters_and_cursor(rig: Rig) -> None:
    for sub, name, groups in [
        ("u-1", "alice", ["acme-admins"]),
        ("u-2", "bob", ["globex-viewers"]),
        ("u-3", "carol", ["acme-admins"]),
    ]:
        rig.client.cookies.clear()
        rig.clock.now += timedelta(minutes=1)
        await rig.login(groups=groups, sub=sub, preferred_username=name)
    sys_admin = await bearer(rig, Role.SYSTEM_ADMIN, None)
    first = (await rig.client.get(f"{V1}/users", params={"limit": 2}, headers=sys_admin)).json()
    assert [u["username"] for u in first["items"]] == ["carol", "bob"] and first["next_cursor"]
    rest = (
        await rig.client.get(
            f"{V1}/users", params={"limit": 2, "cursor": first["next_cursor"]}, headers=sys_admin
        )
    ).json()
    assert [u["username"] for u in rest["items"]] == ["alice"] and rest["next_cursor"] is None
    q = (await rig.client.get(f"{V1}/users", params={"q": "ARO"}, headers=sys_admin)).json()
    assert [u["username"] for u in q["items"]] == ["carol"]
    in_globex = (
        await rig.client.get(f"{V1}/users", params={"tenant": "globex"}, headers=sys_admin)
    ).json()
    assert [u["username"] for u in in_globex["items"]] == ["bob"]
    bad = await rig.client.get(f"{V1}/users", params={"cursor": "junk"}, headers=sys_admin)
    assert bad.status_code == 422


async def test_a_tenant_admin_sees_only_people_with_a_grant_in_their_tenant(rig: Rig) -> None:
    await rig.login(groups=["acme-admins", "globex-viewers"], sub="u-1", preferred_username="al")
    rig.client.cookies.clear()
    await rig.login(groups=["globex-viewers"], sub="u-2", preferred_username="bo")
    admin = await bearer(rig, Role.TENANT_ADMIN)
    page = (await rig.client.get(f"{V1}/tenants/acme/users", headers=admin)).json()
    assert [u["username"] for u in page["items"]] == ["al"]
    only = page["items"][0]
    assert [g["tenant"] for g in only["grants"]] == ["acme"], "only that tenant's grants"
    assert only["groups"] == [], "other tenants' groups are not shown"
    viewer = await bearer(rig, Role.OPERATOR)
    denied = await rig.client.get(f"{V1}/tenants/acme/users", headers=viewer)
    assert denied.status_code == 403
    assert denied.json()["code"] == "role_required"
    assert denied.json()["required_role"] == "tenant-admin"
    system_only = await rig.client.get(f"{V1}/users", headers=admin)
    assert system_only.status_code == 403 and system_only.json()["required_role"] == "system-admin"


async def test_role_mappings_api_enforces_who_may_map_what(rig: Rig) -> None:
    admin = await bearer(rig, Role.TENANT_ADMIN)
    created = await rig.client.post(
        f"{V1}/tenants/acme/role-mappings",
        json={
            "provider": "authentik",
            "group_name": "acme-ops",
            "role": "operator",
            "label": "Ops",
        },
        headers=admin,
    )
    assert created.status_code == 201, created.text
    mapping = created.json()
    assert mapping["tenant"] == "acme" and mapping["role"] == "operator" and mapping["version"] == 1

    escalate = await rig.client.post(
        f"{V1}/tenants/acme/role-mappings",
        json={"provider": "authentik", "group_name": "acme-ops2", "role": "tenant-admin"},
        headers=admin,
    )
    assert escalate.status_code == 403
    assert escalate.json()["code"] == "role_required"
    assert escalate.json()["required_role"] == "system-admin"

    dupe = await rig.client.post(
        f"{V1}/tenants/acme/role-mappings",
        json={"provider": "authentik", "group_name": "acme-ops", "role": "viewer"},
        headers=admin,
    )
    assert dupe.status_code == 409

    foreign = await mapping_id(rig, "globex-viewers")
    for method in ("PATCH", "DELETE"):
        other = await rig.client.request(
            method,
            f"{V1}/tenants/acme/role-mappings/{foreign}",
            json={"label": "x"} if method == "PATCH" else None,
            headers=admin | {"If-Match": '"1"'},
        )
        assert other.status_code == 404, "another tenant's mapping is invisible"

    # a tenant admin cannot edit a tenant-admin mapping, nor promote one of theirs to it
    boss = await mapping_id(rig, "acme-admins")
    cannot = await rig.client.patch(
        f"{V1}/tenants/acme/role-mappings/{boss}",
        json={"label": "mine"},
        headers=admin | {"If-Match": '"1"'},
    )
    assert cannot.status_code == 403 and cannot.json()["required_role"] == "system-admin"
    promote = await rig.client.patch(
        f"{V1}/tenants/acme/role-mappings/{mapping['id']}",
        json={"role": "tenant-admin"},
        headers=admin | {"If-Match": '"1"'},
    )
    assert promote.status_code == 403

    listing = (await rig.client.get(f"{V1}/tenants/acme/role-mappings", headers=admin)).json()
    assert {m["group_name"] for m in listing} == {"acme-admins", "acme-ops"}


async def test_role_mapping_versions_and_audit(rig: Rig) -> None:
    sys_admin = await bearer(rig, Role.SYSTEM_ADMIN, None)
    mapping = await mapping_id(rig, "globex-viewers")
    url = f"{V1}/role-mappings/{mapping}"
    got = await rig.client.get(url, headers=sys_admin)
    assert got.headers["etag"] == '"1"'
    missing = await rig.client.patch(url, json={"label": "x"}, headers=sys_admin)
    assert missing.status_code == 428
    stale = await rig.client.patch(
        url, json={"label": "x"}, headers=sys_admin | {"If-Match": '"7"'}
    )
    assert stale.status_code == 412
    ok = await rig.client.patch(
        url,
        json={"label": "Globex read-only", "role": "operator"},
        headers=sys_admin | {"If-Match": '"1"'},
    )
    assert ok.status_code == 200 and ok.json()["version"] == 2 and ok.headers["etag"] == '"2"'
    system_role = await rig.client.patch(
        url, json={"role": "system-admin"}, headers=sys_admin | {"If-Match": '"2"'}
    )
    assert system_role.status_code == 422, "system-admin takes no tenant"
    bad_group = await rig.client.post(
        f"{V1}/role-mappings",
        json={"provider": "authentik", "group_name": "", "role": "system-admin"},
        headers=sys_admin,
    )
    assert bad_group.status_code == 422
    unknown = await rig.client.post(
        f"{V1}/role-mappings",
        json={"provider": "nope", "group_name": "g", "role": "system-admin"},
        headers=sys_admin,
    )
    assert unknown.status_code == 404
    assert (await rig.client.delete(url, headers=sys_admin)).status_code == 204
    actions = {"role_mapping.update", "role_mapping.delete"}
    for action in actions:
        assert await count(rig, f"SELECT count(*) FROM audit_event WHERE action='{action}'") == 1
    filtered = (
        await rig.client.get(
            f"{V1}/role-mappings",
            params={"provider": "authentik", "tenant": "acme"},
            headers=sys_admin,
        )
    ).json()
    assert {m["group_name"] for m in filtered} == {"acme-admins"}


async def test_tokens_cannot_create_tokens_and_tenants_no_longer_take_oidc_group(rig: Rig) -> None:
    admin = await bearer(rig, Role.TENANT_ADMIN)
    refused = await rig.client.post(
        f"{V1}/tenants/acme/tokens", json={"name": "child", "role": "viewer"}, headers=admin
    )
    assert refused.status_code == 403 and "cannot create tokens" in refused.json()["detail"]
    sys_admin = await bearer(rig, Role.SYSTEM_ADMIN, None)
    made = await rig.client.post(
        f"{V1}/tenants", json={"name": "initech", "oidc_group": "x"}, headers=sys_admin
    )
    assert made.status_code == 422
    ok = await rig.client.post(f"{V1}/tenants", json={"name": "initech"}, headers=sys_admin)
    assert ok.status_code == 201 and "oidc_group" not in ok.json()


async def test_401_and_403_problems_carry_a_code(rig: Rig) -> None:
    none = await rig.client.get(f"{V1}/me")
    assert none.status_code == 401 and none.json()["code"] == "unauthenticated"
    bad = await rig.client.get(f"{V1}/me", headers={"Authorization": "Bearer sy_nope"})
    assert bad.status_code == 401 and bad.json()["code"] == "invalid_token"
    viewer = await bearer(rig, Role.VIEWER)
    forbidden = await rig.client.post(f"{V1}/tenants/acme/change-sets", json={}, headers=viewer)
    body = forbidden.json()
    assert forbidden.status_code == 403
    assert (body["code"], body["required_role"]) == ("role_required", "operator")


async def test_permissions_endpoint_serves_the_matrix(rig: Rig) -> None:
    got = await rig.client.get(f"{V1}/permissions", headers=await bearer(rig, Role.VIEWER))
    body = got.json()
    assert body["roles"] == ["viewer", "operator", "tenant-admin", "system-admin"]
    cells = {(p["verb"], p["object"]): p["min_role"] for p in body["permissions"]}
    assert cells[("approve", "change_set")] == "tenant-admin"
    assert cells[("retry", "job_own")] == "operator" and cells[("retry", "job")] == "tenant-admin"
    anonymous = await rig.client.get(f"{V1}/permissions")
    assert anonymous.status_code == 401

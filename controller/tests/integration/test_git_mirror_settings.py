# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Git mirror settings: validation, secret_ref only, test, and the worker's mirror (ADR 0216)."""

import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest
import pytest_asyncio

from switchyard.adapters.git import GitMirrorError
from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services import git_mirror as module
from switchyard.services.context import Principal
from tests.pipeline_env import GROUP, TENANT, World, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

V1 = "/api/v1"
ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
BODY = {
    "enabled": True,
    "url": "https://git.example.test",
    "project": "ops/mirror",
    "branch": "main",
    "token_ref": "kv/git/token",
}


class FakeGitLab:
    """Stands in for GitLabMirror; records what it was built with."""

    built: list[tuple[str, str, str, str]] = []
    fail = False

    def __init__(self, url: str, project: str, token: str, *, branch: str = "main") -> None:
        FakeGitLab.built.append((url, project, token, branch))

    async def head(self) -> str:
        if FakeGitLab.fail:
            raise GitMirrorError("reading branch: HTTP 404")
        return "a" * 40

    async def commit(self, prefix: str, files: Mapping[str, str], message: str) -> str:
        if FakeGitLab.fail:
            raise GitMirrorError("commit failed: HTTP 500")
        return "b" * 40

    async def close(self) -> None:
        return None


class Rig:
    def __init__(self, w: World, services: ApiServices, client: httpx.AsyncClient) -> None:
        self.w, self.services, self.client = w, services, client

    async def token(self, role: Role) -> dict[str, str]:
        tenant = None if role is Role.SYSTEM_ADMIN else TENANT
        plaintext, _ = await self.services.tokens.create(
            ROOT, tenant, f"t{uuid.uuid4().hex[:8]}", role
        )
        return {"Authorization": f"Bearer {plaintext}"}


@pytest_asyncio.fixture
async def rig(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    for name in ("URL", "PROJECT", "TOKEN"):
        monkeypatch.delenv(f"SWITCHYARD_GITLAB_{name}", raising=False)
    monkeypatch.setattr(module, "GitLabMirror", FakeGitLab)
    FakeGitLab.built, FakeGitLab.fail = [], False
    async with world(pg_url) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        services = build_services(w.db, directory, MemorySecrets({"kv/git/token": "glpat-test"}))
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            yield Rig(w, services, client)


async def test_unconfigured_reports_none_and_writes_nothing(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    got = (await rig.client.get(f"{V1}/system/git-mirror", headers=root)).json()
    assert got["source"] == "none" and got["enabled"] is False
    assert await rig.services.git_mirror.commit("tenants/a/", {}, "m") == ""
    assert FakeGitLab.built == []


async def test_saving_reads_the_token_from_the_secret_ref_only(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    put = await rig.client.put(f"{V1}/system/git-mirror", json=BODY, headers=root)
    assert put.status_code == 200, put.text
    assert put.json()["source"] == "settings" and put.json()["token_ref"] == "kv/git/token"
    assert "glpat" not in put.text
    sha = await rig.services.git_mirror.commit("tenants/a/", {"x": "y"}, "m")
    assert sha == "b" * 40
    assert FakeGitLab.built == [("https://git.example.test", "ops/mirror", "glpat-test", "main")]
    got = (await rig.client.get(f"{V1}/system/git-mirror", headers=root)).json()
    assert got["last_sha"] == "b" * 40 and got["last_error"] is None and got["last_ok_at"]


async def test_a_failed_write_is_remembered_and_raised(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.client.put(f"{V1}/system/git-mirror", json=BODY, headers=root)
    FakeGitLab.fail = True
    with pytest.raises(GitMirrorError):
        await rig.services.git_mirror.commit("tenants/a/", {}, "m")
    got = (await rig.client.get(f"{V1}/system/git-mirror", headers=root)).json()
    assert got["last_error"] == "commit failed: HTTP 500"


async def test_disabled_does_nothing(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.client.put(f"{V1}/system/git-mirror", json={**BODY, "enabled": False}, headers=root)
    assert await rig.services.git_mirror.commit("tenants/a/", {}, "m") == ""
    assert FakeGitLab.built == []


async def test_environment_is_the_fallback_until_settings_are_saved(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    monkeypatch.setenv("SWITCHYARD_GITLAB_URL", "https://env.example.test")
    monkeypatch.setenv("SWITCHYARD_GITLAB_PROJECT", "env/mirror")
    monkeypatch.setenv("SWITCHYARD_GITLAB_TOKEN", "glpat-env")
    got = (await rig.client.get(f"{V1}/system/git-mirror", headers=root)).json()
    assert got["source"] == "environment" and got["url"] == "https://env.example.test"
    assert "glpat-env" not in str(got)
    await rig.services.git_mirror.commit("tenants/a/", {}, "m")
    assert FakeGitLab.built[-1][2] == "glpat-env"
    await rig.client.put(f"{V1}/system/git-mirror", json=BODY, headers=root)
    await rig.services.git_mirror.commit("tenants/a/", {}, "m")
    assert FakeGitLab.built[-1][2] == "glpat-test"


async def test_test_reads_the_branch_and_reports_failures(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    none = (await rig.client.post(f"{V1}/system/git-mirror/test", headers=root)).json()
    assert none["ok"] is False and none["error"] == "Not configured."
    await rig.client.put(f"{V1}/system/git-mirror", json=BODY, headers=root)
    ok = (await rig.client.post(f"{V1}/system/git-mirror/test", headers=root)).json()
    assert ok["ok"] is True and ok["head"] == "a" * 40
    FakeGitLab.fail = True
    bad = (await rig.client.post(f"{V1}/system/git-mirror/test", headers=root)).json()
    assert bad["ok"] is False and "404" in bad["error"]
    FakeGitLab.fail = False
    await rig.client.put(
        f"{V1}/system/git-mirror", json={**BODY, "token_ref": "kv/git/missing"}, headers=root
    )
    missing = (await rig.client.post(f"{V1}/system/git-mirror/test", headers=root)).json()
    assert missing["error"] == "no secret at kv/git/missing"


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"url": "http://git.example.test"}, "url"),
        ({"url": "https://u:p@git.example.test"}, "url"),
        ({"project": "a/../b"}, "project"),
        ({"branch": "-x"}, "branch"),
        ({"token_ref": "glpat xyz!"}, "token_ref"),
        ({"token_ref": ""}, "token_ref"),
    ],
)
async def test_settings_are_validated(rig: Rig, over: dict[str, Any], field: str) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    res = await rig.client.put(f"{V1}/system/git-mirror", json={**BODY, **over}, headers=root)
    assert res.status_code == 422 and field in res.text


async def test_only_system_admins_may_use_it(rig: Rig) -> None:
    tenant_admin = await rig.token(Role.TENANT_ADMIN)
    for call in (
        rig.client.get(f"{V1}/system/git-mirror", headers=tenant_admin),
        rig.client.put(f"{V1}/system/git-mirror", json=BODY, headers=tenant_admin),
        rig.client.post(f"{V1}/system/git-mirror/test", headers=tenant_admin),
    ):
        assert (await call).status_code == 403

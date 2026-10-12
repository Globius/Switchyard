# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Git mirror: deterministic YAML export and the GitLab commit logic (HTTP mocked)."""

import json
from typing import Any

import httpx
import pytest

from switchyard.adapters.git import FakeGitMirror, GitLabMirror, GitMirrorError, revision_files
from tests.factories import base_snapshot


def test_export_is_deterministic_and_complete() -> None:
    first = revision_files("acme", base_snapshot())
    assert first == revision_files("acme", base_snapshot())
    assert sorted(first) == [
        "tenants/acme/certificates/wild-example-test.yaml",
        "tenants/acme/networks/backend.yaml",
        "tenants/acme/networks/lb.yaml",
        "tenants/acme/pools/web.yaml",
        "tenants/acme/se_groups/edge.yaml",
        "tenants/acme/tenant.yaml",
        "tenants/acme/vip_pools/main.yaml",
        "tenants/acme/virtual_services/web.yaml",
    ]


def test_export_holds_references_not_secrets() -> None:
    files = revision_files("acme", base_snapshot())
    cert = files["tenants/acme/certificates/wild-example-test.yaml"]
    assert "secret_ref: kv/tenants/acme/certs/wild-example-test" in cert
    assert "BEGIN" not in "".join(files.values())


async def test_fake_mirror_commits_only_real_changes() -> None:
    mirror = FakeGitMirror()
    first = await mirror.commit("tenants/a/", {"tenants/a/x.yaml": "1"}, "one")
    assert await mirror.commit("tenants/a/", {"tenants/a/x.yaml": "1"}, "again") == first
    assert len(mirror.commits) == 1
    await mirror.commit("tenants/a/", {"tenants/a/y.yaml": "2"}, "two")
    assert mirror.tree == {"tenants/a/y.yaml": "2"}, "files that disappeared are deleted"


def gitlab(handler: Any) -> GitLabMirror:
    client = httpx.AsyncClient(
        base_url="https://git.example.test/api/v4", transport=httpx.MockTransport(handler)
    )
    return GitLabMirror("https://git.example.test", "group/mirror", "token", client=client)


async def test_gitlab_creates_updates_and_deletes() -> None:
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repository/tree"):
            return httpx.Response(
                200,
                json=[
                    {"path": "tenants/a/keep.yaml", "type": "blob"},
                    {"path": "tenants/a/old.yaml", "type": "blob"},
                    {"path": "tenants/a/dir", "type": "tree"},
                ],
            )
        assert request.url.raw_path.decode() == "/api/v4/projects/group%2Fmirror/repository/commits"
        assert request.headers["PRIVATE-TOKEN"] == "token"
        posted.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "abc123"})

    mirror = gitlab(handler)
    sha = await mirror.commit(
        "tenants/a/", {"tenants/a/keep.yaml": "k", "tenants/a/new.yaml": "n"}, "msg"
    )
    assert sha == "abc123"
    assert posted[0]["branch"] == "main"
    assert posted[0]["actions"] == [
        {"action": "update", "file_path": "tenants/a/keep.yaml", "content": "k"},
        {"action": "create", "file_path": "tenants/a/new.yaml", "content": "n"},
        {"action": "delete", "file_path": "tenants/a/old.yaml"},
    ]


async def test_gitlab_paginates_the_tree_and_handles_a_missing_directory() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repository/tree"):
            page = int(request.url.params["page"])
            pages.append(page)
            if page == 1:
                return httpx.Response(
                    200, json=[{"path": f"p/{i}.yaml", "type": "blob"} for i in range(100)]
                )
            return httpx.Response(404)
        return httpx.Response(201, json={"id": "sha"})

    mirror = gitlab(handler)
    await mirror.commit("p/", {"p/0.yaml": "x"}, "m")
    assert pages == [1, 2]


async def test_gitlab_without_changes_returns_the_head_and_does_not_commit() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + request.url.path)
        if request.url.path.endswith("/repository/tree"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"commit": {"id": "head"}})

    assert await gitlab(handler).commit("p/", {}, "m") == "head"
    assert not any(c.startswith("POST") for c in calls)


async def test_gitlab_errors_are_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repository/tree"):
            return httpx.Response(200, json=[])
        return httpx.Response(400, text="bad things")

    with pytest.raises(GitMirrorError, match="HTTP 400"):
        await gitlab(handler).commit("p/", {"p/a.yaml": "x"}, "m")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(GitMirrorError, match="ConnectError"):
        await gitlab(boom).commit("p/", {"p/a.yaml": "x"}, "m")

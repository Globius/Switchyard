# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Git mirror adapter: every committed revision is written as YAML to a GitLab repository.

PostgreSQL stays the source of truth. The mirror exists for review, audit and disaster recovery, so
its failures raise alerts but never block an apply (the apply pipeline only *queues* the mirror
job).

Layout of the repository: ``tenants/<tenant>/<collection>/<name>.yaml`` with one file per object and
a ``tenants/<tenant>/tenant.yaml``. Only ``secret_ref`` strings appear, never secrets.
"""

from collections.abc import Mapping
from typing import Protocol

import httpx
import yaml

from switchyard.domain.changes import COLLECTION
from switchyard.domain.snapshot import Snapshot


class GitMirrorError(RuntimeError):
    """The mirror could not be updated."""


class GitMirror(Protocol):
    """Writes the files of one revision to a repository."""

    async def commit(self, prefix: str, files: Mapping[str, str], message: str) -> str:
        """Make everything under ``prefix`` equal to ``files`` in one commit; return its sha.

        Files under ``prefix`` that are not in ``files`` are deleted. If nothing changes, no commit
        is made and the current head sha is returned.
        """
        ...


def revision_files(tenant_name: str, snapshot: Snapshot) -> dict[str, str]:
    """Return the YAML files of a revision, keyed by repository path.

    The output is deterministic (sorted keys, objects by name) so identical state gives identical
    files and commits only contain real changes.

    Example:
        >>> from switchyard.domain.loader import snapshot_from_mapping
        >>> snap = snapshot_from_mapping({"tenants": [{"id": "t", "name": "acme"}]})
        >>> sorted(revision_files("acme", snap))
        ['tenants/acme/tenant.yaml']
    """
    files: dict[str, str] = {}
    for tenant in snapshot.tenants:
        files[f"tenants/{tenant_name}/tenant.yaml"] = _dump(tenant.model_dump(mode="json"))
    for name in COLLECTION.values():
        for obj in sorted(getattr(snapshot, name), key=lambda o: (o.name, str(o.id))):
            files[f"tenants/{tenant_name}/{name}/{obj.name}.yaml"] = _dump(
                obj.model_dump(mode="json")
            )
    return files


def _dump(data: object) -> str:
    return yaml.safe_dump(data, sort_keys=True, default_flow_style=False, allow_unicode=True)


class FakeGitMirror:
    """Keeps the repository in memory, for tests."""

    def __init__(self) -> None:
        """Create an empty repository."""
        self.tree: dict[str, str] = {}
        self.commits: list[tuple[str, str]] = []
        self.fail_next = False

    async def commit(self, prefix: str, files: Mapping[str, str], message: str) -> str:
        """Replace the files under ``prefix``; no-op commits return the head."""
        if self.fail_next:
            self.fail_next = False
            raise GitMirrorError("injected failure")
        current = {p: c for p, c in self.tree.items() if p.startswith(prefix)}
        if current == dict(files):
            return self.commits[-1][0] if self.commits else ""
        for path in current:
            del self.tree[path]
        self.tree.update(files)
        sha = f"{len(self.commits) + 1:040x}"
        self.commits.append((sha, message))
        return sha


class GitLabMirror:
    """:class:`GitMirror` using the GitLab REST API (repository commits endpoint)."""

    def __init__(
        self,
        base_url: str,
        project_id: str,
        token: str,
        *,
        branch: str = "main",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create the mirror.

        Args:
            base_url: GitLab URL, for example ``https://git.example.org``.
            project_id: Numeric id or URL-encoded path of the mirror project.
            token: Deploy token / project access token with write access to that repository only.
            branch: Branch that receives the commits.
            client: Pre-built client (tests).
        """
        self._project = project_id.replace("/", "%2F")
        self._branch = branch
        self._headers = {"PRIVATE-TOKEN": token}
        self._http = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v4", timeout=30
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()

    async def _tree(self, prefix: str) -> set[str]:
        paths: set[str] = set()
        page = 1
        while True:
            response = await self._http.get(
                f"/projects/{self._project}/repository/tree",
                params={
                    "path": prefix.rstrip("/"),
                    "ref": self._branch,
                    "recursive": "true",
                    "per_page": 100,
                    "page": page,
                },
                headers=self._headers,
            )
            if response.status_code == 404:
                return paths  # the directory does not exist yet
            if response.status_code != 200:
                raise GitMirrorError(f"listing {prefix}: HTTP {response.status_code}")
            items = response.json()
            paths.update(i["path"] for i in items if i["type"] == "blob")
            if len(items) < 100:
                return paths
            page += 1

    async def commit(self, prefix: str, files: Mapping[str, str], message: str) -> str:
        """Commit the difference between the repository and ``files`` under ``prefix``."""
        try:
            existing = await self._tree(prefix)
            actions = [
                {
                    "action": "update" if path in existing else "create",
                    "file_path": path,
                    "content": content,
                }
                for path, content in sorted(files.items())
            ] + [{"action": "delete", "file_path": path} for path in sorted(existing - set(files))]
            if not actions:
                return await self._head()
            response = await self._http.post(
                f"/projects/{self._project}/repository/commits",
                json={"branch": self._branch, "commit_message": message, "actions": actions},
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            raise GitMirrorError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code != 201:
            raise GitMirrorError(
                f"commit failed: HTTP {response.status_code}: {response.text[:300]}"
            )
        return str(response.json()["id"])

    async def head(self) -> str:
        """The sha of the mirror branch's head; also what the settings page's Test reads.

        Raises:
            GitMirrorError: The project or branch cannot be read with this token.
        """
        try:
            return await self._head()
        except httpx.HTTPError as exc:
            raise GitMirrorError(f"{type(exc).__name__}: {exc}") from exc

    async def _head(self) -> str:
        response = await self._http.get(
            f"/projects/{self._project}/repository/branches/{self._branch}",
            headers=self._headers,
        )
        if response.status_code != 200:
            raise GitMirrorError(f"reading branch: HTTP {response.status_code}")
        return str(response.json()["commit"]["id"])

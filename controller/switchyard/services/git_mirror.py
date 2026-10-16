# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Git mirror settings: where committed revisions are written, and how that is going (ADR 0216).

The settings live in ``system_setting`` (key ``git_mirror``). The access token is never stored:
only ``token_ref``, a reference into the secrets backend, which is read each time the mirror
writes. When nothing is stored, the older ``SWITCHYARD_GITLAB_*`` environment variables still
work, so existing deployments keep mirroring. A mirror that is not configured does nothing: a
revision is applied either way (the apply only queues the mirror job).

System administrators only (permission ``manage controller``).

Example:
    >>> check_config({"enabled": False}).enabled
    False
"""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import select

from switchyard.adapters.git import GitLabMirror, GitMirrorError
from switchyard.adapters.secrets import SecretBackendError, SecretNotFoundError, SecretsReader
from switchyard.db import audit_log
from switchyard.db.auth_models import SystemSettingRow
from switchyard.db.tenancy import admin_session
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import FieldErrors

log = logging.getLogger(__name__)

CONFIG_KEY = "git_mirror"
STATE_KEY = "git_mirror_state"
_PROJECT = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
_REF = re.compile(r"^[A-Za-z0-9._/:-]{1,200}$")


Source = Literal["settings", "environment", "none"]


@dataclass(frozen=True)
class MirrorConfig:
    """Operator settings for the mirror."""

    enabled: bool = False
    url: str = ""
    project: str = ""
    branch: str = "main"
    token_ref: str = ""


@dataclass(frozen=True)
class MirrorState:
    """What the last write did."""

    last_ok_at: str | None = None
    last_sha: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class MirrorView:
    """The settings, where they come from, and the state."""

    config: MirrorConfig
    source: Source
    state: MirrorState


def check_config(body: Mapping[str, Any]) -> MirrorConfig:
    """Validate settings from the API.

    Raises:
        FieldErrors: One entry per invalid field.

    Example:
        >>> check_config({"url": "https://git.example.test", "project": "ops/mirror",
        ...               "token_ref": "kv/git/token", "enabled": True}).branch
        'main'
    """
    errors = FieldErrors()
    url = str(body.get("url", "")).strip()
    parts = urlsplit(url)
    if url and (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        errors.add("url", "invalid", "must be an https URL without credentials")
    project = str(body.get("project", "")).strip()
    if project and (not _PROJECT.fullmatch(project) or ".." in project):
        errors.add("project", "invalid", "a numeric id or a path such as group/project")
    branch = str(body.get("branch") or "main").strip()
    if not _BRANCH.fullmatch(branch) or ".." in branch or branch.startswith("-"):
        errors.add("branch", "invalid", "not a valid branch name")
    token_ref = str(body.get("token_ref", "")).strip()
    if token_ref and not _REF.fullmatch(token_ref):
        errors.add("token_ref", "invalid", "a secret reference such as kv/git/token, not a token")
    enabled = bool(body.get("enabled"))
    if enabled:
        for field, value in (("url", url), ("project", project), ("token_ref", token_ref)):
            if not value:
                errors.add(field, "required", "needed to enable the mirror")
    errors.raise_if_any()
    return MirrorConfig(enabled, url, project, branch, token_ref)


def _env_config() -> MirrorConfig | None:
    url = os.environ.get("SWITCHYARD_GITLAB_URL", "").strip()
    project = os.environ.get("SWITCHYARD_GITLAB_PROJECT", "").strip()
    token = os.environ.get("SWITCHYARD_GITLAB_TOKEN", "").strip()
    if not (url and project and token):
        return None
    return MirrorConfig(True, url, project, "main", "")


class GitMirrorSettings:
    """Configure the Git mirror, check it, and build the mirror the worker writes with."""

    def __init__(self, db: Databases, secrets: SecretsReader | None) -> None:
        """``secrets`` resolves ``token_ref``."""
        self._db, self._secrets = db, secrets

    async def _get(self, key: str) -> Any:
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(select(SystemSettingRow).where(SystemSettingRow.key == key))
            ).scalar_one_or_none()
        return None if row is None else row.value

    async def _put(self, session: Any, key: str, value: Any, actor: str) -> None:
        row = await session.get(SystemSettingRow, key)
        if row is None:
            session.add(SystemSettingRow(key=key, value=value, updated_by=actor))
        else:
            row.value, row.updated_by = value, actor

    async def _state(self) -> MirrorState:
        raw = await self._get(STATE_KEY) or {}
        return MirrorState(raw.get("last_ok_at"), raw.get("last_sha"), raw.get("last_error"))

    async def _effective(self) -> tuple[MirrorConfig | None, Source]:
        raw = await self._get(CONFIG_KEY)
        if raw is not None:
            cfg = MirrorConfig(
                bool(raw.get("enabled", False)),
                raw.get("url", ""),
                raw.get("project", ""),
                raw.get("branch", "main"),
                raw.get("token_ref", ""),
            )
            return cfg, "settings"
        env = _env_config()
        return (env, "environment") if env else (None, "none")

    async def view(self, principal: Principal) -> MirrorView:
        """The current settings and state. Requires a system administrator."""
        require(principal, "manage", "controller")
        cfg, source = await self._effective()
        return MirrorView(
            cfg or MirrorConfig(),
            source,
            await self._state(),
        )

    async def set_config(
        self, principal: Principal, body: Mapping[str, Any], *, request_id: str | None = None
    ) -> MirrorView:
        """Replace the settings. Raises :class:`FieldErrors` for invalid ones."""
        require(principal, "manage", "controller")
        cfg = check_config(body)
        async with admin_session(self._db.admin) as session:
            await self._put(
                session,
                CONFIG_KEY,
                {
                    "enabled": cfg.enabled,
                    "url": cfg.url,
                    "project": cfg.project,
                    "branch": cfg.branch,
                    "token_ref": cfg.token_ref,
                },
                principal.actor,
            )
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="git_mirror.configured",
                object_ref="setting:git_mirror",
                diff={
                    "enabled": cfg.enabled,
                    "url": cfg.url,
                    "project": cfg.project,
                    "branch": cfg.branch,
                    "token_ref": cfg.token_ref,
                },
                request_id=request_id,
            )
        return await self.view(principal)

    async def _build(self, cfg: MirrorConfig, source: Source) -> GitLabMirror:
        if source == "environment":
            token = os.environ["SWITCHYARD_GITLAB_TOKEN"]
        else:
            if self._secrets is None:
                raise GitMirrorError("no secrets backend is configured")
            try:
                token = await self._secrets.get(cfg.token_ref)
            except SecretNotFoundError:
                raise GitMirrorError(f"no secret at {cfg.token_ref}") from None
            except SecretBackendError as exc:
                raise GitMirrorError(f"the secrets backend is unavailable: {exc}") from None
        return GitLabMirror(cfg.url, cfg.project, token, branch=cfg.branch)

    async def test(self, principal: Principal) -> dict[str, Any]:
        """Read the mirror branch with the stored credentials. Writes nothing.

        Returns ``{"ok": True, "head": sha}`` or ``{"ok": False, "error": reason}``.
        """
        require(principal, "manage", "controller")
        cfg, source = await self._effective()
        if cfg is None or not (
            cfg.url and cfg.project and (cfg.token_ref or source == "environment")
        ):
            return {"ok": False, "error": "Not configured."}
        try:
            mirror = await self._build(cfg, source)
            try:
                return {"ok": True, "head": await mirror.head()}
            finally:
                await mirror.close()
        except GitMirrorError as exc:
            return {"ok": False, "error": str(exc)}

    async def record(self, *, sha: str | None = None, error: str | None = None) -> None:
        """Remember the outcome of a write for the settings page."""
        prior = await self._state()
        now = datetime.now(UTC).isoformat()
        value = (
            {"last_ok_at": now, "last_sha": sha or prior.last_sha, "last_error": None}
            if error is None
            else {"last_ok_at": prior.last_ok_at, "last_sha": prior.last_sha, "last_error": error}
        )
        async with admin_session(self._db.admin) as session:
            await self._put(session, STATE_KEY, value, "system:git_mirror")

    async def commit(self, prefix: str, files: Mapping[str, str], message: str) -> str:
        """Write a revision with the current settings (the :class:`GitMirror` the worker uses).

        Returns ``""`` without writing when the mirror is off or not configured. Failures raise
        :class:`GitMirrorError`, are remembered for the settings page, and the job retries.
        """
        cfg, source = await self._effective()
        if cfg is None or not cfg.enabled:
            return ""
        try:
            mirror = await self._build(cfg, source)
            try:
                sha = await mirror.commit(prefix, files, message)
            finally:
                await mirror.close()
        except GitMirrorError as exc:
            await self.record(error=str(exc))
            raise
        await self.record(sha=sha)
        return sha

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Response models of the upgrade check API (ADR 0205)."""

from typing import Literal

from pydantic import Field

from switchyard.api.schemas import ApiModel


class EngineVersionRead(ApiModel):
    """An engine and the agent version it last reported."""

    engine_id: str
    name: str
    tenant_id: str
    agent_version: str | None = Field(description="`null` until the agent has reported.")


class FindingRead(ApiModel):
    """One finding of the upgrade check."""

    code: str = Field(description="Stable code, for example `agent_behind`.")
    severity: Literal["info", "warning", "error"]
    subject: str = Field(description="What it is about: an engine name or `database`.")
    message: str
    action: str = Field(description="What to do about it; empty when nothing is needed.")


class UpgradeCheckRead(ApiModel):
    """Versions found and what to do about them. Nothing here blocks an engine or a request."""

    controller_version: str
    latest_version: str = Field(
        description="The newest known release (this controller until a feed says otherwise)."
    )
    schema_current: str | None
    schema_head: str
    schema_state: Literal["current", "pending", "newer"]
    engines: list[EngineVersionRead]
    findings: list[FindingRead]


class BackupRunRead(ApiModel):
    """One backup attempt. A failed run keeps the reason but never any credential."""

    id: str
    target: str
    archive: str | None
    status: Literal["ok", "failed"]
    error: str | None
    size_bytes: int
    sha256: str | None
    controller_version: str
    schema_revision: str | None
    tenants: int
    reason: str
    actor: str
    created_at: str


class BackupRunIn(ApiModel):
    """Which destination to back up to."""

    target: str = Field(description="The name of a backup target.")
    reason: Literal["manual", "pre-upgrade", "scheduled"] = "manual"


class BackupVerifyIn(ApiModel):
    """Which archive to open."""

    target: str
    archive: str = Field(description="An archive name as listed by the target, `.syb`.")


class BackupTargetTestRead(ApiModel):
    """The archives found at a destination."""

    archives: list[str]

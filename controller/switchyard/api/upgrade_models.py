# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Response models of the upgrade check API (ADR 0205)."""

from datetime import datetime
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


class ReleaseNotesRead(ApiModel):
    """What a release changes, in the groups an operator reads before confirming."""

    new: list[str] = Field(default_factory=list)
    deprecated: list[str] = Field(default_factory=list)
    breaking: list[str] = Field(default_factory=list)
    action_required: list[str] = Field(default_factory=list)


class BundleRead(ApiModel):
    """An upgrade bundle to download and what it must hash to."""

    url: str
    sha256: str
    size_bytes: int


class ReleaseRead(ApiModel):
    """One release from the verified feed."""

    version: str
    channel: Literal["stable", "candidate"] = "stable"
    date: str
    notes_url: str | None = None
    notes: ReleaseNotesRead = Field(default_factory=ReleaseNotesRead)
    upgrade_from: list[str] = Field(default_factory=list)
    min_agent: str = "0.0.0"
    recommended_min_agent: str = "0.0.0"
    bundle: BundleRead | None = None


class UpdateRead(ApiModel):
    """The newer release the feed found and how to get there."""

    version: str
    date: str
    notes_url: str | None
    notes: ReleaseNotesRead
    path: list[str] = Field(
        description="Versions to install in order; one element is a direct step."
    )
    bundle: BundleRead | None


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
    update: UpdateRead | None = Field(
        default=None,
        description="The newer release the feed found: version, notes, upgrade path and bundle.",
    )


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


class BackupTenantSummary(ApiModel):
    """Object counts of one tenant inside an archive."""

    name: str
    virtual_services: int
    pools: int
    se_groups: int
    certificates: int


class BackupVerifyRead(ApiModel):
    """What a decrypted archive holds. Counts only; no object content is returned."""

    controller_version: str
    schema_revision: str | None
    created_at: str
    tenants: list[BackupTenantSummary]


class BackupRestoreIn(ApiModel):
    """Which archive to load."""

    target: str
    archive: str
    dry_run: bool = Field(
        default=False, description="Report what would be restored; write nothing."
    )


class BackupRestoreRead(BackupVerifyRead):
    """What the archive holds, and whether it was restored."""

    restored: bool


class ReleaseFeedIn(ApiModel):
    """Where to look for newer releases. Off until `enabled` is true."""

    url: str = Field(default="", description="An https URL of the signed `releases.json`.")
    channel: Literal["stable", "candidate"] = "stable"
    enabled: bool = False
    trusted_keys: dict[str, str] = Field(
        default_factory=dict,
        description="Extra trusted signing keys: key id to base64 Ed25519 public key (32 bytes).",
    )


class ReleaseFeedRead(ReleaseFeedIn):
    """The feed settings and what the last fetch found."""

    checked_at: str | None = Field(default=None, description="When the feed was last fetched.")
    ok: bool | None = Field(default=None, description="Whether a verified feed is stored.")
    error: str | None = Field(default=None, description="Why the last fetch failed, if it did.")
    latest_version: str | None = None
    releases: list[ReleaseRead] = Field(
        default_factory=list, description="Verified releases, with notes and upgrade paths."
    )


class RollStartIn(ApiModel):
    """Replace every enabled engine of an SE group with one cloned from a new template."""

    template_vmid: int = Field(ge=100, description="The Proxmox template to clone.")
    target_version: str | None = Field(
        default=None, description="The release this template carries, for the record."
    )
    window_start: str | None = Field(
        default=None,
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="Start the next engine only after this time of day, UTC (`HH:MM`).",
    )
    window_end: str | None = Field(
        default=None,
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="...and before this time. Earlier than the start means across midnight.",
    )
    window_days: list[int] = Field(
        default_factory=list,
        max_length=7,
        description="Weekdays the window applies to, 0 (Monday) to 6; empty for every day.",
    )
    start_after: datetime | None = Field(
        default=None, description="Do not start before this moment (timezone-aware)."
    )
    soak_seconds: int = Field(
        default=0, ge=0, le=86400, description="Wait this long after each engine before the next."
    )


class RollActionIn(ApiModel):
    """What to do with a roll."""

    action: Literal["pause", "resume", "abort"]


class RollStepRead(ApiModel):
    """One engine of a roll."""

    old: str
    new: str | None
    phase: str


class RollRead(ApiModel):
    """A roll and where it is."""

    id: str
    se_group_id: str
    status: Literal["running", "paused", "aborted", "done"]
    reason: str = Field(description="Why it paused or is holding, when it is.")
    window: str | None = Field(
        default=None, description="The start window, for example `02:00-05:00 UTC`."
    )
    not_before: float | None = Field(default=None, description="Scheduled start, epoch seconds.")
    soak_seconds: int = 0
    holding: bool = Field(default=False, description="Waiting for the window, start time or soak.")
    template_vmid: int
    target_version: str | None
    steps: list[RollStepRead]
    log: list[str]
    created_by: str
    created_at: str
    updated_at: str
    version: int

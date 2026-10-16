# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Request and response models of the CrowdSec integration API (ADR 0213)."""

from typing import Literal

from pydantic import Field

from switchyard.api.schemas import ApiModel


class CrowdSecIn(ApiModel):
    """The CrowdSec integration settings. An empty `url` removes the stored settings."""

    url: str = Field(
        default="",
        description="Base URL of the CrowdSec local API (http or https, no credentials).",
    )
    api_key: str | None = Field(
        default=None,
        description=(
            "A bouncer key to store. It is written to the secrets backend and never returned or "
            "kept in the database. Leave out to keep the current reference."
        ),
    )
    key_ref: str = Field(
        default="",
        description="Reference (OpenBao path) of a bouncer key stored there already.",
    )
    origins: list[str] = Field(
        default_factory=list,
        description="Keep only decisions from these origins: `crowdsec`, `CAPI`, `cscli`, `lists`.",
    )
    logs_address: str = Field(
        default="",
        description=(
            "`host:port` (UDP) of the CrowdSec syslog datasource that receives the access logs of "
            "services that opted in with `share_logs`. Empty turns the feed off."
        ),
    )


class CrowdSecRead(ApiModel):
    """The settings in effect and how the last read of the decision stream went."""

    url: str
    key_ref: str = Field(description="Where the bouncer key is stored; the key is never shown.")
    origins: list[str]
    logs_address: str
    source: Literal["database", "environment", "none"] = Field(
        description="Where the settings come from; the environment is the fallback."
    )
    configured: bool
    synced_at: str | None = Field(default=None, description="When the stream was last read.")
    ok: bool | None = Field(default=None, description="Whether the last read worked.")
    bans: int | None = Field(default=None, description="Networks banned at the last good read.")
    error: str | None = Field(default=None, description="Why the last read failed, if it did.")


class CrowdSecTestRead(ApiModel):
    """The outcome of one test read."""

    ok: bool
    bans: int | None = None
    error: str | None = None


class CrowdSecSyncRead(ApiModel):
    """The job queued by Sync now."""

    job_id: str

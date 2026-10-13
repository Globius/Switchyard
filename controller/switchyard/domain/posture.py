# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Device posture: the facts a device agent reports and the check against a policy (ADR 0196).

Pure functions and models; the agent, the signing and the certificates are elsewhere. A report is
*claims by the device*: it is only as honest as the device, which is why the result is a
short-lived client certificate, not a permanent grant (ADR 0196 spells out the limits).

Example:
    >>> facts = PostureFacts(os="linux", os_version="6.8.0", disk_encrypted=True,
    ...                      firewall_enabled=True, days_since_update=3)
    >>> evaluate(PostureRequirements(), facts)
    []
    >>> evaluate(PostureRequirements(require_disk_encryption=True),
    ...          facts.model_copy(update={"disk_encrypted": False}))
    ['disk_not_encrypted']
"""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OsName = Literal["windows", "macos", "linux"]
_VERSION = re.compile(r"^[0-9]{1,6}(\.[0-9]{1,6}){0,3}$")
Version = Annotated[str, Field(pattern=_VERSION.pattern)]


def version_key(version: str) -> tuple[int, ...]:
    """A comparable key for a dotted numeric version; missing parts count as zero.

    Example:
        >>> version_key("10.0.19045") > version_key("10.0.9")
        True
        >>> version_key("14") == version_key("14.0.0")
        True
    """
    if not _VERSION.fullmatch(version):
        raise ValueError(f"not a version: {version!r}")
    parts = [int(p) for p in version.split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OsMinimum(_Strict):
    """The oldest version of one operating system that is accepted."""

    os: OsName
    version: Version


class PostureRequirements(_Strict):
    """What a device must show to get a certificate.

    Attributes:
        allowed_os: Operating systems that may enroll.
        minimums: Oldest accepted version per operating system (none: no check).
        require_disk_encryption: BitLocker, FileVault or LUKS on the system volume.
        require_firewall: The host firewall is on.
        require_screen_lock: A screen lock is configured (macOS and Windows; Linux reports it
            when the desktop exposes it, otherwise the check fails).
        max_patch_age_days: Days since the last operating system update was installed
            (none: no check).
    """

    allowed_os: tuple[OsName, ...] = Field(default=("windows", "macos", "linux"), min_length=1)
    minimums: tuple[OsMinimum, ...] = Field(default=(), max_length=3)
    require_disk_encryption: bool = True
    require_firewall: bool = True
    require_screen_lock: bool = False
    max_patch_age_days: int | None = Field(default=45, ge=1, le=365)

    @model_validator(mode="after")
    def _consistent(self) -> "PostureRequirements":
        if len(set(self.allowed_os)) != len(self.allowed_os):
            raise ValueError("allowed_os must be unique")
        names = [m.os for m in self.minimums]
        if len(set(names)) != len(names) or not set(names) <= set(self.allowed_os):
            raise ValueError("minimums must be unique and for allowed operating systems")
        return self


class PostureFacts(_Strict):
    """What the device agent measured. Unknown fields are refused, so the format is explicit.

    Attributes:
        os / os_version: Operating system and its dotted numeric version.
        disk_encrypted: Full-disk encryption is on for the system volume.
        firewall_enabled: The host firewall is on.
        screen_lock: A screen lock is configured; ``None`` when the agent cannot tell.
        days_since_update: Days since the last update was installed; ``None`` when unknown.
        agent_version: Version of the agent that reported.
        hostname: Informational; never used for a decision.
    """

    os: OsName
    os_version: Version
    disk_encrypted: bool
    firewall_enabled: bool
    screen_lock: bool | None = None
    days_since_update: int | None = Field(default=None, ge=0, le=36500)
    agent_version: Annotated[str, Field(pattern=r"^[A-Za-z0-9._+-]{1,32}$")] = "0"
    hostname: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,253}$")] | None = None


def evaluate(requirements: PostureRequirements, facts: PostureFacts) -> list[str]:
    """The codes of every requirement the facts do not meet (empty: compliant).

    Codes: ``os_not_allowed``, ``os_too_old``, ``disk_not_encrypted``, ``firewall_off``,
    ``screen_lock_off``, ``screen_lock_unknown``, ``patch_age_unknown``, ``patch_too_old``.
    Unknown facts fail the requirement that needs them: a device that cannot show it is
    compliant is not.

    Example:
        >>> req = PostureRequirements(minimums=[OsMinimum(os="windows", version="10.0.19045")],
        ...                           require_screen_lock=True)
        >>> f = PostureFacts(os="windows", os_version="10.0.19044", disk_encrypted=True,
        ...                  firewall_enabled=False, days_since_update=100)
        >>> evaluate(req, f)
        ['os_too_old', 'firewall_off', 'screen_lock_unknown', 'patch_too_old']
    """
    failures: list[str] = []
    if facts.os not in requirements.allowed_os:
        return ["os_not_allowed"]
    for minimum in requirements.minimums:
        if minimum.os == facts.os and version_key(facts.os_version) < version_key(minimum.version):
            failures.append("os_too_old")
    if requirements.require_disk_encryption and not facts.disk_encrypted:
        failures.append("disk_not_encrypted")
    if requirements.require_firewall and not facts.firewall_enabled:
        failures.append("firewall_off")
    if requirements.require_screen_lock:
        if facts.screen_lock is None:
            failures.append("screen_lock_unknown")
        elif not facts.screen_lock:
            failures.append("screen_lock_off")
    if requirements.max_patch_age_days is not None:
        if facts.days_since_update is None:
            failures.append("patch_age_unknown")
        elif facts.days_since_update > requirements.max_patch_age_days:
            failures.append("patch_too_old")
    return failures

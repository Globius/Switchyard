# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Settings that a system administrator controls for the whole controller.

Today there is one: whether LXC guests may be deployed (``allow_lxc_guests``, default off). A
privileged container shares the Proxmox host's kernel, so it is a lab option; production turns it
on only on purpose (ADR 0076). ``SWITCHYARD_ALLOW_LXC=1`` forces it on for a deployment (labs and
the quickstart), the way ``SWITCHYARD_BREAK_GLASS=1`` forces break-glass login.
"""

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from switchyard.db import audit_log
from switchyard.db.auth_models import SystemSettingRow
from switchyard.db.tenancy import admin_session
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal

ALLOW_LXC = "allow_lxc_guests"
#: Message of the refusal; the API adds the code ``lxc_disabled``.
LXC_DISABLED = (
    "Container (LXC) guests are disabled on this controller. A system administrator can enable "
    "them under system settings."
)


@dataclass(frozen=True)
class SettingsView:
    """The settings as they are in effect.

    Attributes:
        allow_lxc_guests: Whether an LXC deploy is accepted now (stored value or forced).
        allow_lxc_guests_forced: True when ``SWITCHYARD_ALLOW_LXC=1`` forces it on.
    """

    allow_lxc_guests: bool
    allow_lxc_guests_forced: bool


def lxc_forced() -> bool:
    """True when ``SWITCHYARD_ALLOW_LXC=1`` is set.

    Example:
        >>> os.environ["SWITCHYARD_ALLOW_LXC"] = "1"
        >>> lxc_forced()
        True
        >>> del os.environ["SWITCHYARD_ALLOW_LXC"]
        >>> lxc_forced()
        False
    """
    return os.environ.get("SWITCHYARD_ALLOW_LXC") == "1"


class SystemSettings:
    """Read and change the controller-wide settings."""

    def __init__(self, db: Databases) -> None:
        """Create the service."""
        self._db = db

    async def _stored(self, key: str) -> Any:
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(select(SystemSettingRow).where(SystemSettingRow.key == key))
            ).scalar_one_or_none()
        return None if row is None else row.value

    async def lxc_allowed(self) -> bool:
        """Whether a deploy with ``guest_kind: lxc`` is accepted (no permission check)."""
        return lxc_forced() or bool(await self._stored(ALLOW_LXC))

    async def view(self) -> SettingsView:
        """The effective settings (anyone signed in may see whether containers are offered)."""
        return SettingsView(await self.lxc_allowed(), lxc_forced())

    async def update(
        self,
        principal: Principal,
        *,
        allow_lxc_guests: bool | None = None,
        request_id: str | None = None,
    ) -> SettingsView:
        """Change settings (system administrators only); ``None`` leaves a setting alone.

        Raises:
            PermissionDeniedError: If the principal is not a system administrator.
        """
        require(principal, "manage", "tenant")
        if allow_lxc_guests is not None:
            async with admin_session(self._db.admin) as session:
                row = (
                    await session.execute(
                        select(SystemSettingRow)
                        .where(SystemSettingRow.key == ALLOW_LXC)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                before = None if row is None else row.value
                if row is None:
                    session.add(
                        SystemSettingRow(
                            key=ALLOW_LXC, value=allow_lxc_guests, updated_by=principal.actor
                        )
                    )
                else:
                    row.value, row.updated_by = allow_lxc_guests, principal.actor
                await audit_log.record(
                    session,
                    tenant_id=None,
                    actor=principal.actor,
                    action="system.settings_changed",
                    object_ref=f"setting:{ALLOW_LXC}",
                    diff={"from": before, "to": allow_lxc_guests},
                    request_id=request_id,
                )
        return await self.view()

    async def set_value(self, key: str, value: bool, actor: str = "cli:setting") -> None:
        """Write a setting without a permission check (the command line, with database access)."""
        if key != ALLOW_LXC:
            raise ValueError(f"unknown setting {key!r}; known: {ALLOW_LXC}")
        async with admin_session(self._db.admin) as session:
            row = await session.get(SystemSettingRow, key)
            if row is None:
                session.add(SystemSettingRow(key=key, value=value, updated_by=actor))
            else:
                row.value, row.updated_by = value, actor
            await audit_log.record(
                session,
                tenant_id=None,
                actor=actor,
                action="system.settings_changed",
                object_ref=f"setting:{key}",
                diff={"to": value},
            )

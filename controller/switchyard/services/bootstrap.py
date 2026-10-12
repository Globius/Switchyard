# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Objects every controller needs, created when it starts (ADR 0155).

The locked ``management`` tenant (ADR 0051) holds the ``controller`` virtual service and its SE
group. Creating the tenant itself is harmless and idempotent, so the controller does it at start-up:
the settings screen then knows the tenant exists and can say what is still missing (the
``controller`` service, which needs a VIP and engines that only a deployment or an ``adopt``
file can provide), instead of reporting that the tenant is absent.

Example:
    >>> MANAGEMENT_TENANT
    'management'
"""

import logging

from sqlalchemy import select

from switchyard.db import audit_log
from switchyard.db.models import TenantRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.management import MANAGEMENT_TENANT
from switchyard.ids import uuid7
from switchyard.services.context import Databases

log = logging.getLogger("switchyard.bootstrap")

ACTOR = "system:bootstrap"


async def ensure_management_tenant(db: Databases) -> bool:
    """Create the ``management`` tenant when it does not exist. Returns True if it was created.

    Safe to call on every start and from several controllers at once: a concurrent insert that
    loses the race is ignored.
    """
    async with admin_session(db.admin) as session:
        if (
            await session.execute(select(TenantRow.id).where(TenantRow.name == MANAGEMENT_TENANT))
        ).first():
            return False
        row = TenantRow(id=uuid7(), name=MANAGEMENT_TENANT, quotas={}, require_approval=False)
        session.add(row)
        try:
            await session.flush()
        except Exception:  # a second controller created it first (unique name)
            await session.rollback()
            return False
        await audit_log.record(
            session,
            tenant_id=None,  # system-level, so adopting over the empty tenant leaves no trace
            actor=ACTOR,
            action="tenant.create",
            object_ref=f"tenant:{MANAGEMENT_TENANT}",
            request_id=None,
        )
    log.info("created the %s tenant", MANAGEMENT_TENANT)
    return True

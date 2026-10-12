# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Audit trail: every write, apply, rollback and drain is an ``audit_event`` row and a log line.

The row is written in the caller's transaction, so an audited action and its record commit or roll
back together. The log line (logger ``switchyard.audit``, one JSON object) is what goes to syslog.
"""

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from switchyard.db.ops_models import AuditEventRow
from switchyard.ids import uuid7

log = logging.getLogger("switchyard.audit")


async def record(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    actor: str,
    action: str,
    object_ref: str | None = None,
    diff: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """Append an audit event in the current transaction and emit it to the log.

    Args:
        session: Session whose transaction will hold the row.
        tenant_id: Tenant the action concerns; ``None`` for system-level actions (admin session).
        actor: Who acted, for example ``token:ci-bot``.
        action: Dotted action name, for example ``change_set.apply``.
        object_ref: Affected object as ``kind:id`` or ``kind:name``.
        diff: Structured details. Must not contain secrets.
        request_id: Request id that flows from the API into jobs.
    """
    session.add(
        AuditEventRow(
            id=uuid7(),
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            object_ref=object_ref,
            diff=diff,
            request_id=request_id,
        )
    )
    log.info(
        json.dumps(
            {
                "event": "audit",
                "tenant_id": str(tenant_id) if tenant_id else None,
                "actor": actor,
                "action": action,
                "object": object_ref,
                "request_id": request_id,
            },
            sort_keys=True,
        )
    )

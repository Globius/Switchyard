# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Display names for the principal strings that audit events, change sets and revisions carry.

An ``actor``, ``author`` or ``approver`` is a label such as ``user:3f9a...`` (the provider's subject
id), ``token:ci-bot`` or ``local:admin``. :func:`display_names` turns a batch of them into names a
person can read. Users get the name their identity provider gave at sign-in; everything else is
named from the label itself, so every input has an answer.

Inside a tenant, only principals that appear in that tenant's own history (audit events, change
sets, revisions) are resolved to a real name; any other ``user:`` label is answered from the label
alone, so the lookup cannot be used to enumerate people of other tenants.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, union

from switchyard.db.auth_models import UserAccountRow
from switchyard.db.ops_models import AuditEventRow, ChangeSetRow, RevisionRow
from switchyard.db.tenancy import admin_session
from switchyard.services.context import Databases

#: Most labels one call resolves.
MAX_ACTORS = 100


@dataclass(frozen=True)
class PrincipalName:
    """One resolved label.

    Attributes:
        actor: The label as given.
        kind: ``user``, ``token``, ``local`` (break-glass), ``login`` (a sign-in attempt),
            ``system`` or ``unknown``.
        display_name: A name for people. Never empty.
    """

    actor: str
    kind: str
    display_name: str


_KINDS = {"user", "token", "local", "login", "system"}


def fallback(actor: str) -> PrincipalName:
    """Name a label without looking anything up.

    Example:
        >>> fallback("token:ci-bot")
        PrincipalName(actor='token:ci-bot', kind='token', display_name='ci-bot (API token)')
        >>> fallback("system:drift_detector").display_name
        'drift detector (system)'
        >>> fallback("mystery").kind
        'unknown'
    """
    kind, _, rest = actor.partition(":")
    if kind not in _KINDS or not rest:
        return PrincipalName(actor, "unknown", actor or "unknown")
    names = {
        "user": rest,
        "token": f"{rest} (API token)",
        "local": f"{rest} (break-glass)",
        "login": f"sign-in via {rest}",
        "system": f"{rest.replace('_', ' ')} (system)",
    }
    return PrincipalName(actor, kind, names[kind])


async def display_names(
    db: Databases, actors: list[str], *, tenant_id: UUID | None = None
) -> list[PrincipalName]:
    """Resolve ``actors`` in order, without duplicates.

    Args:
        db: Database handles (the admin role reads the user table).
        actors: Labels, at most :data:`MAX_ACTORS` distinct ones.
        tenant_id: Limit user lookups to principals seen in this tenant; ``None`` (system
            administrators) looks every user up.

    Raises:
        ValueError: If there are more than :data:`MAX_ACTORS` distinct labels.
    """
    distinct = list(dict.fromkeys(a for a in actors if a))
    if len(distinct) > MAX_ACTORS:
        raise ValueError(f"at most {MAX_ACTORS} actors per request")
    wanted = [a for a in distinct if a.startswith("user:")]
    names: dict[str, str] = {}
    if wanted:
        async with admin_session(db.admin) as session:
            allowed = set(wanted)
            if tenant_id is not None:
                seen: Any = union(
                    select(AuditEventRow.actor).where(
                        AuditEventRow.tenant_id == tenant_id, AuditEventRow.actor.in_(wanted)
                    ),
                    select(ChangeSetRow.author).where(
                        ChangeSetRow.tenant_id == tenant_id, ChangeSetRow.author.in_(wanted)
                    ),
                    select(ChangeSetRow.approver).where(
                        ChangeSetRow.tenant_id == tenant_id, ChangeSetRow.approver.in_(wanted)
                    ),
                    select(RevisionRow.author).where(
                        RevisionRow.tenant_id == tenant_id, RevisionRow.author.in_(wanted)
                    ),
                )
                allowed = set((await session.execute(seen)).scalars())
            subjects = [a.removeprefix("user:") for a in allowed]
            rows = await session.execute(
                select(UserAccountRow.subject, UserAccountRow.username).where(
                    UserAccountRow.subject.in_(subjects)
                )
            )
            for subject, username in rows:
                names.setdefault(f"user:{subject}", username)
    return [PrincipalName(a, "user", names[a]) if a in names else fallback(a) for a in distinct]

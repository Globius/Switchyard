# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tenant search (command palette) and the tenant home summary.

Both are read-only views computed from the desired state, the stored engine status and a few
counters; the functions here are pure so they are easy to test, and the routes only gather inputs.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import Field

from switchyard.api.schemas import ApiModel, VirtualServiceHealth
from switchyard.domain.changes import COLLECTION, ObjectType
from switchyard.domain.snapshot import Snapshot
from switchyard.services.status import EngineStatus, VirtualServiceStatus, is_up

#: Certificates this close to expiry are listed on the tenant home.
EXPIRY_WARNING = timedelta(days=30)
#: Cap on the lists in the summary; the full data is behind the linked collections.
LIST_CAP = 50


class SearchHit(ApiModel):
    """One match of `GET /search`."""

    type: ObjectType
    id: UUID
    name: str
    matched: str = Field(description="What matched: `name`, `vip`, `san` or `address`.")
    detail: str | None = Field(
        default=None, description="Context to show next to the name, for example the VIP."
    )


class SearchResults(ApiModel):
    """Matches of a search, best first (exact name, prefix, then substring; then by name)."""

    query: str
    hits: list[SearchHit]
    truncated: bool = Field(description="True when more matches exist than `limit`.")


class ServiceHealthCounts(ApiModel):
    """Virtual services by health."""

    total: int
    healthy: int = 0
    degraded: int = 0
    down: int = 0
    unknown: int = 0
    disabled: int = 0


class FailingMember(ApiModel):
    """A pool member that a reachable engine reports as not `UP`."""

    engine: str
    engine_id: UUID
    backend: str
    server: str
    status: str
    check_status: str | None = Field(
        default=None, description="Short code of the last health check, for example `L7STS`."
    )
    last_check: str | None = Field(
        default=None, description="HAProxy description of the last health check."
    )
    last_change_s: int | None = Field(
        default=None, description="Seconds since the member status last changed."
    )
    last_checked_at: datetime = Field(
        description="When the controller last read this engine's check results."
    )


class DriftedEngine(ApiModel):
    """An engine whose running configuration differs from the desired state."""

    engine: str
    engine_id: UUID
    se_group_id: UUID
    kinds: list[str]


class UnreachableEngine(ApiModel):
    """An enabled engine the controller could not poll."""

    engine: str
    engine_id: UUID
    se_group_id: UUID
    error: str | None = None
    observed_at: datetime


class ExpiringCertificate(ApiModel):
    """A certificate that expires within 30 days (or already has)."""

    id: UUID
    name: str
    not_after: datetime
    days_left: int = Field(description="Whole days until expiry; negative once expired.")


class JobCounts(ApiModel):
    """The tenant's jobs by state (all time, from the queue table)."""

    queued: int = 0
    running: int = 0
    failed: int = Field(default=0, description="Failed or dead jobs.")


class LatestRevision(ApiModel):
    """The newest revision."""

    number: int
    author: str
    summary: str
    created_at: datetime


class TenantSummary(ApiModel):
    """Everything the tenant home shows, in one call."""

    services: ServiceHealthCounts
    failing_members: list[FailingMember]
    drifted_engines: list[DriftedEngine]
    unreachable_engines: list[UnreachableEngine]
    expiring_certificates: list[ExpiringCertificate]
    jobs: JobCounts
    open_drafts: int = Field(description="Change sets in state `draft`.")
    pending_approval: int = Field(description="Change sets in state `pending_approval`.")
    latest_revision: LatestRevision | None = None


def search(
    snapshot: Snapshot, query: str, types: Sequence[ObjectType], limit: int
) -> SearchResults:
    """Find objects by name (and by VIP, certificate SAN or pool member address).

    Matching is case-insensitive and by substring. Results are ordered exact name first, then
    prefix, then substring, then by name, so the best match is at the top of the palette.

    Example:
        >>> search(snapshot, "web", list(ObjectType), 5).hits[0].name  # doctest: +SKIP
        'web'
    """
    needle = query.strip().lower()
    scored: list[tuple[int, str, SearchHit]] = []
    if needle:
        for object_type in types:
            for obj in getattr(snapshot, COLLECTION[object_type]):
                name: str = obj.name
                matched, detail, rank = None, None, 9
                lowered = name.lower()
                if lowered == needle:
                    matched, rank = "name", 0
                elif lowered.startswith(needle):
                    matched, rank = "name", 1
                elif needle in lowered:
                    matched, rank = "name", 2
                if object_type is ObjectType.VIRTUAL_SERVICE:
                    detail = str(obj.vip)
                    if matched is None and needle in detail.lower():
                        matched, rank = "vip", 3
                elif object_type is ObjectType.CERTIFICATE and matched is None:
                    if any(needle in san.lower() for san in obj.sans):
                        matched, rank = "san", 3
                elif object_type is ObjectType.POOL and matched is None:
                    if any(needle in str(m.address) for m in obj.members):
                        matched, rank = "address", 3
                if matched is not None:
                    hit = SearchHit(
                        type=object_type, id=obj.id, name=name, matched=matched, detail=detail
                    )
                    scored.append((rank, name.lower(), hit))
    scored.sort(key=lambda t: (t[0], t[1], str(t[2].id)))
    return SearchResults(
        query=query,
        hits=[h for _, _, h in scored[:limit]],
        truncated=len(scored) > limit,
    )


def summarize(
    *,
    snapshot: Snapshot,
    statuses: Sequence[EngineStatus],
    services: Sequence[VirtualServiceStatus],
    jobs: JobCounts,
    open_drafts: int,
    pending_approval: int,
    latest_revision: LatestRevision | None,
    now: datetime,
) -> TenantSummary:
    """Combine the pieces into the tenant home summary.

    Failing members are the rows a reachable engine reports as not ``UP`` (a drained member is
    listed too, because it takes no traffic). Lists are capped at 50 and sorted for stability.
    """
    counts = ServiceHealthCounts(total=len(services))
    for s in services:
        health = VirtualServiceHealth(s.health).value
        setattr(counts, health, getattr(counts, health) + 1)
    failing = [
        FailingMember(
            engine=e.name,
            engine_id=e.engine_id,
            backend=m["backend"],
            server=m["server"],
            status=m["status"],
            check_status=m.get("check_status"),
            last_check=m.get("last_check"),
            last_change_s=int(m["last_change_s"]) if "last_change_s" in m else None,
            last_checked_at=e.observed_at,
        )
        for e in statuses
        if e.reachable
        for m in e.members
        if not is_up(m["status"])
    ]
    drifted = [
        DriftedEngine(
            engine=e.name,
            engine_id=e.engine_id,
            se_group_id=e.se_group_id,
            kinds=sorted({d["kind"] for d in e.drift}),
        )
        for e in statuses
        if e.drift
    ]
    unreachable = [
        UnreachableEngine(
            engine=e.name,
            engine_id=e.engine_id,
            se_group_id=e.se_group_id,
            error=e.error,
            observed_at=e.observed_at,
        )
        for e in statuses
        if not e.reachable
    ]
    expiring = sorted(
        (
            ExpiringCertificate(
                id=c.id,
                name=c.name,
                not_after=c.not_after,
                days_left=(c.not_after - now).days,
            )
            for c in snapshot.certificates
            if c.not_after - now <= EXPIRY_WARNING
        ),
        key=lambda c: (c.not_after, c.name),
    )
    return TenantSummary(
        services=counts,
        failing_members=sorted(failing, key=lambda f: (f.backend, f.server, f.engine))[:LIST_CAP],
        drifted_engines=sorted(drifted, key=lambda d: d.engine)[:LIST_CAP],
        unreachable_engines=sorted(unreachable, key=lambda u: u.engine)[:LIST_CAP],
        expiring_certificates=expiring[:LIST_CAP],
        jobs=jobs,
        open_drafts=open_drafts,
        pending_approval=pending_approval,
        latest_revision=latest_revision,
    )

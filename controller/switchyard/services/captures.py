# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Packet captures on service engines (ADR 0194).

A tenant administrator asks for a capture on one engine: an interface (or ``any``), an optional
protocol, host and port, a duration and limits. The request is validated here and again by the
agent, stored, and run by the ``packet_capture`` job, which streams tcpdump's pcap file from the
engine's agent into the database. Nothing here builds a tcpdump command: the agent builds the
filter from these fields. A capture holds real traffic, so it needs the engine-management role,
is audited when it is requested and when it is downloaded, allows one capture per engine at a time
and is deleted after :data:`RETENTION` by housekeeping.
"""

import ipaddress
import logging
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from switchyard.adapters.agent import AgentError, CaptureSpec
from switchyard.db import audit_log
from switchyard.db.ops_models import PacketCaptureRow, ServiceEngineRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.workflow import EngineState
from switchyard.ids import uuid7
from switchyard.jobs import queue
from switchyard.pipeline.engines import EngineDirectory, EngineRecord
from switchyard.services.changesets import require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import (
    ConflictError,
    FieldErrors,
    NotFoundError,
    PermissionDeniedError,
)

log = logging.getLogger("switchyard.captures")

PACKET_CAPTURE_JOB = "packet_capture"
#: How long a capture is kept before housekeeping deletes it.
RETENTION = timedelta(hours=24)
#: The most pcap bytes kept for one capture; the agent stops sending at the same limit.
MAX_BYTES = 16 << 20
MAX_DURATION_S = 60
MAX_PACKETS = 20_000
_INTERFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


def validate_spec(spec: CaptureSpec) -> CaptureSpec:
    """Check a capture request field by field.

    Example:
        >>> validate_spec(CaptureSpec(interface="eth0", protocol="tcp", port=443)).port
        443

    Raises:
        FieldErrors: With the path of every invalid field.
    """
    errors = FieldErrors()
    if spec.interface != "any" and not _INTERFACE.match(spec.interface):
        errors.add("interface", "invalid", "use `any` or an interface name such as eth0")
    if spec.protocol not in ("", "tcp", "udp", "icmp"):
        errors.add("protocol", "invalid", "use tcp, udp or icmp, or leave it empty")
    if spec.host:
        try:
            ipaddress.ip_address(spec.host)
        except ValueError:
            errors.add("host", "invalid", "use an IP address")
    if not 0 <= spec.port <= 65535:
        errors.add("port", "invalid", "use 1 to 65535, or 0 for any port")
    elif spec.port and spec.protocol == "icmp":
        errors.add("port", "invalid", "a port cannot be combined with icmp")
    if not 1 <= spec.duration_s <= MAX_DURATION_S:
        errors.add("duration_s", "invalid", f"use 1 to {MAX_DURATION_S} seconds")
    if not 1 <= spec.max_packets <= MAX_PACKETS:
        errors.add("max_packets", "invalid", f"use 1 to {MAX_PACKETS}")
    if spec.snaplen and not 64 <= spec.snaplen <= 262_144:
        errors.add("snaplen", "invalid", "use 64 to 262144 bytes, or 0 for whole packets")
    errors.raise_if_any()
    return spec


def _view(row: PacketCaptureRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "engine": row.engine_name,
        "requested_by": row.requested_by,
        "spec": dict(row.spec),
        "state": row.state,
        "packets": row.packets,
        "size_bytes": row.size_bytes,
        "truncated": row.truncated,
        "error": row.error,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
        "expires_at": row.expires_at,
    }


class PacketCaptures:
    """Requests, runs, lists and serves packet captures."""

    def __init__(
        self, db: Databases, directory: EngineDirectory | None = None, *, clock: Clock = utcnow
    ) -> None:
        """Create the service; the directory is only needed by the job (:meth:`run`)."""
        self._db = db
        self._directory = directory
        self._clock = clock

    @staticmethod
    def _check(principal: Principal, tenant_id: UUID) -> None:
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, "manage", "engine")

    async def request(
        self,
        principal: Principal,
        tenant_id: UUID,
        engine_id: UUID,
        spec: CaptureSpec,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate and queue a capture on an engine; returns the new capture.

        Raises:
            FieldErrors: If a field is invalid.
            NotFoundError: If the engine does not exist.
            ConflictError: If the engine is disabled or already has a capture queued or running.
        """
        self._check(principal, tenant_id)
        validate_spec(spec)
        now = self._clock()
        async with tenant_session(self._db.app, [tenant_id]) as session:
            engine = await session.get(ServiceEngineRow, engine_id)
            if engine is None:
                raise NotFoundError(f"engine {engine_id} does not exist")
            if engine.state != EngineState.ENABLED.value or not engine.agent_url:
                raise ConflictError(f"engine {engine.name} is disabled or has no agent")
            busy = (
                await session.execute(
                    select(PacketCaptureRow.id).where(
                        PacketCaptureRow.engine_id == engine_id,
                        PacketCaptureRow.state.in_(("queued", "running")),
                    )
                )
            ).first()
            if busy:
                raise ConflictError(f"engine {engine.name} already has a capture in progress")
            row = PacketCaptureRow(
                id=uuid7(),
                tenant_id=tenant_id,
                engine_id=engine_id,
                engine_name=engine.name,
                requested_by=principal.actor,
                spec=asdict(spec),
                expires_at=now + RETENTION,
            )
            session.add(row)
            await session.flush()
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="capture.requested",
                object_ref=f"engine:{engine.name}",
                diff=asdict(spec),
                request_id=request_id,
            )
            await queue.enqueue(
                session,
                PACKET_CAPTURE_JOB,
                {"tenant_id": str(tenant_id), "capture_id": str(row.id)},
                tenant_id=tenant_id,
                max_attempts=1,
                request_id=request_id,
            )
            return _view(row)

    async def list_captures(self, principal: Principal, tenant_id: UUID) -> list[dict[str, Any]]:
        """The tenant's captures, newest first (without the pcap data)."""
        self._check(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = (
                (
                    await session.execute(
                        select(PacketCaptureRow).order_by(PacketCaptureRow.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [_view(r) for r in rows]

    async def get(self, principal: Principal, tenant_id: UUID, capture_id: UUID) -> dict[str, Any]:
        """One capture's state."""
        self._check(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await session.get(PacketCaptureRow, capture_id)
            if row is None:
                raise NotFoundError(f"capture {capture_id} does not exist")
            return _view(row)

    async def download(
        self,
        principal: Principal,
        tenant_id: UUID,
        capture_id: UUID,
        *,
        request_id: str | None = None,
    ) -> tuple[str, bytes]:
        """The pcap file as ``(file name, bytes)``; audited.

        Raises:
            NotFoundError: If the capture does not exist or has no data yet (or any more).
        """
        self._check(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await session.get(PacketCaptureRow, capture_id)
            if row is None or row.data is None:
                raise NotFoundError(f"capture {capture_id} has no data")
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="capture.downloaded",
                object_ref=f"engine:{row.engine_name}",
                diff={"capture": str(row.id), "bytes": row.size_bytes},
                request_id=request_id,
            )
            stamp = row.created_at.strftime("%Y%m%d-%H%M%S")
            return f"{row.engine_name}-{stamp}.pcap", bytes(row.data)

    async def run(self, tenant_id: UUID, capture_id: UUID) -> dict[str, Any]:
        """The ``packet_capture`` job: capture on the engine and store the pcap.

        A failure is recorded on the capture (state ``failed`` with the reason) instead of raising,
        so the job itself succeeds and the person sees the reason where they asked.
        """
        if self._directory is None:
            raise RuntimeError("the capture job needs an engine directory")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await session.get(PacketCaptureRow, capture_id)
            if row is None:
                return {"state": "gone"}
            engine = await session.get(ServiceEngineRow, row.engine_id)
            if engine is None:
                return await self._finish(tenant_id, capture_id, error="the engine was removed")
            row.state = "running"
            spec = CaptureSpec(**row.spec)
            record = EngineRecord(
                id=engine.id,
                tenant_id=tenant_id,
                se_group_id=engine.se_group_id,
                name=engine.name,
                dataplane_url=engine.dataplane_url,
                credentials_ref=engine.credentials_ref,
                state=EngineState(engine.state),
                agent_url=engine.agent_url,
                mgmt_ip=str(engine.mgmt_ip).split("/")[0] if engine.mgmt_ip else None,
            )
        agent = await self._directory.connect_agent(record)
        if agent is None:
            return await self._finish(tenant_id, capture_id, error="the engine has no agent")
        buffer = bytearray()

        async def sink(chunk: bytes) -> None:
            if len(buffer) + len(chunk) > MAX_BYTES:
                raise AgentError("the capture is larger than the limit")
            buffer.extend(chunk)

        try:
            result = await agent.capture(spec, sink)
        except AgentError as exc:
            return await self._finish(tenant_id, capture_id, error=str(exc), data=bytes(buffer))
        finally:
            await agent.close()
        return await self._finish(
            tenant_id,
            capture_id,
            error=result.error,
            data=bytes(buffer),
            packets=result.packets,
            truncated=result.truncated,
        )

    async def _finish(
        self,
        tenant_id: UUID,
        capture_id: UUID,
        *,
        error: str | None = None,
        data: bytes | None = None,
        packets: int | None = None,
        truncated: bool = False,
    ) -> dict[str, Any]:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await session.get(PacketCaptureRow, capture_id)
            if row is None:
                return {"state": "gone"}
            row.state = "failed" if error else "done"
            row.error = error
            row.data = data or None
            row.size_bytes = len(data) if data else 0
            row.packets = packets
            row.truncated = truncated
            row.finished_at = self._clock()
            return {"state": row.state, "bytes": row.size_bytes, "packets": packets}


async def expire(db: Databases, now: datetime) -> int:
    """Delete captures past their retention; returns how many.

    Used by housekeeping. Takes the admin role because it spans tenants.
    """
    from sqlalchemy import delete

    async with admin_session(db.admin) as session:
        result = await session.execute(
            delete(PacketCaptureRow).where(PacketCaptureRow.expires_at < now)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

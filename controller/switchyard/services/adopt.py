"""Adopt infrastructure that was built outside the controller (the management tenant bootstrap).

The first controller VMs and the management SE group are built by OpenTofu and cloud-init, before
any controller exists (02 "Bootstrap"). ``syctl adopt`` then loads a description of them as ordinary
objects, so they show up in the UI like any other tenant, and records the engines (and the Proxmox
node and VM id they run on) without deploying anything.

The file is a snapshot mapping like ``switchyard/examples/demo.yaml`` plus an optional ``engines``
list and an optional ``controller_members`` list: the controller VMs, including the external one on
ESXi (ADR 0051), which sit outside the tenant's objects. Ids may be symbolic; they are replaced with
fresh UUIDv7 values.

Example:
    ``tenants: [{id: mgmt, name: management}]``, networks, a VIP pool, an SE group and a virtual
    service named ``controller`` whose pool lists the controller VMs, then
    ``engines: [{se_group: edge, name: se-1, dataplane_url: "https://10.0.0.21:5555"}]``.
"""

from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from switchyard.db.controller_models import ControllerMemberRow
from switchyard.db.models import NetworkRow, SeGroupRow, TenantRow, VirtualServiceRow
from switchyard.db.ops_models import ServiceEngineRow
from switchyard.db.repository import add_snapshot, find_tenant_id
from switchyard.db.tenancy import admin_session
from switchyard.domain.controller_members import check_member, is_external
from switchyard.domain.loader import reassign_ids, snapshot_from_mapping
from switchyard.domain.management import MANAGEMENT_TENANT
from switchyard.domain.snapshot import Snapshot, ensure_valid
from switchyard.ids import uuid7
from switchyard.services.engines import validate_dataplane_url
from switchyard.services.errors import AlreadyExistsError


def load_adoption(text: str) -> tuple[Snapshot, list[dict[str, Any]]]:
    """Parse and validate an adoption file. Returns the snapshot and the engine descriptions.

    Raises:
        ValueError: If the file is not a mapping or an engine names an unknown SE group.
        switchyard.domain.errors.DomainValidationError: If the objects are not valid together.
    """
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("the adoption file must be a YAML mapping")
    engines = list(data.pop("engines", None) or [])
    data.pop("controller_members", None)  # read by load_controller_members
    snapshot = reassign_ids(snapshot_from_mapping(data), uuid7)
    ensure_valid(snapshot)
    groups = {g.name for g in snapshot.se_groups}
    for engine in engines:
        if engine.get("se_group") not in groups:
            raise ValueError(f"engine {engine.get('name')!r} names an unknown SE group")
        validate_dataplane_url(engine["dataplane_url"])
    return snapshot, engines


def load_controller_members(text: str) -> list[dict[str, Any]]:
    """The ``controller_members`` of an adoption file, validated.

    Each entry takes ``name``, ``address``, ``platform`` and optionally ``host``, ``external``,
    ``nofailover`` and ``patroni_url`` (the fields of ``POST /system/controller/members``).

    Raises:
        ValueError: If an entry is not valid or two entries share a name.
    """
    data = yaml.safe_load(text)
    members = list((data or {}).get("controller_members") or []) if isinstance(data, dict) else []
    names: set[str] = set()
    for member in members:
        if not isinstance(member, dict):
            raise ValueError("controller_members entries must be mappings")
        name = str(member.get("name"))
        problems = check_member(
            name,
            str(member.get("address")),
            str(member.get("platform")),
            member.get("patroni_url"),
            member.get("host"),
        )
        if problems:
            raise ValueError(f"controller member {name!r}: {problems[0][0]}: {problems[0][1]}")
        if name in names:
            raise ValueError(f"controller member {name!r} is listed twice")
        names.add(name)
    return members


async def _is_bootstrap_shell(session: AsyncSession, tenant_id: UUID, name: str) -> bool:
    """True for the empty management tenant created at start-up: nothing in it yet."""
    if name != MANAGEMENT_TENANT:
        return False
    for model in (SeGroupRow, VirtualServiceRow, NetworkRow, ServiceEngineRow):
        found = await session.execute(select(model.id).where(model.tenant_id == tenant_id).limit(1))
        if found.first():
            return False
    return True


async def adopt(admin: AsyncEngine, path: Path) -> tuple[Snapshot, int]:
    """Insert the objects and engines from ``path``. Returns the snapshot and the engine count.

    Raises:
        AlreadyExistsError: If a tenant with the same name exists (adopt never merges).
    """
    text = path.read_text("utf-8")
    snapshot, engines = load_adoption(text)
    members = load_controller_members(text)
    tenant = snapshot.tenants[0]
    group_ids = {g.name: g.id for g in snapshot.se_groups}
    async with admin_session(admin) as session:
        existing = await find_tenant_id(session, tenant.name)
        if existing is not None:
            if not await _is_bootstrap_shell(session, existing, tenant.name):
                raise AlreadyExistsError(f"tenant {tenant.name!r} already exists")
            # the empty `management` tenant the controller created at start-up (ADR 0155)
            await session.execute(delete(TenantRow).where(TenantRow.id == existing))
            await session.flush()
        await add_snapshot(session, snapshot)
        for engine in engines:
            session.add(
                ServiceEngineRow(
                    id=uuid7(),
                    tenant_id=tenant.id,
                    se_group_id=group_ids[engine["se_group"]],
                    name=engine["name"],
                    dataplane_url=engine["dataplane_url"],
                    agent_url=engine.get("agent_url"),
                    mgmt_ip=engine.get("mgmt_ip"),
                    credentials_ref=engine.get("credentials_ref"),
                    pve_node=engine.get("pve_node"),
                    vmid=engine.get("vmid"),
                )
            )
        for member in members:
            if (
                await session.execute(
                    select(ControllerMemberRow.id).where(ControllerMemberRow.name == member["name"])
                )
            ).first():
                raise AlreadyExistsError(f"controller member {member['name']!r} already exists")
            session.add(
                ControllerMemberRow(
                    id=uuid7(),
                    name=member["name"],
                    address=member["address"],
                    platform=member["platform"],
                    host=member.get("host"),
                    external=member.get("external", is_external(member["platform"])),
                    nofailover=bool(member.get("nofailover", False)),
                    patroni_url=(member.get("patroni_url") or "").rstrip("/") or None,
                    created_by="syctl adopt",
                )
            )
        await session.flush()
    return snapshot, len(engines)

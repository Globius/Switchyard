"""The FastAPI application: ``/api/v1``.

Authentication is a bearer API token (see :mod:`switchyard.services.tokens`); OIDC/SAML login will
issue short-lived sessions in front of the same principal model. Errors are RFC 9457 problem+json.
Writes to desired state always go through a change set: ``?change_set=<id>`` adds an operation to a
draft, ``?apply=true`` opens an implicit single-operation change set and applies it.
"""

import hmac
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from switchyard.adapters.agent import AgentError
from switchyard.adapters.pki import PkiError
from switchyard.adapters.proxmox import Proxmox
from switchyard.adapters.secrets import SecretsReader, SecretsWriter
from switchyard.adapters.telemetry import Telemetry, TelemetryError
from switchyard.api import docs_meta, job_models, overview
from switchyard.api import examples as ex
from switchyard.api.overview import SearchResults, TenantSummary
from switchyard.api.problems import ApiError, install_handlers
from switchyard.api.rendered_models import RenderedConfig, RenderedFragment
from switchyard.api.schemas import (
    ETAG_HEADER,
    IF_MATCH_RESPONSES,
    OBJECT_APIS,
    PROBLEM_RESPONSES,
    VALIDATION_RESPONSE,
    Accepted,
    AccessLogs,
    AcmePresetRead,
    AcmeProviderIn,
    AcmeProviderPatch,
    AcmeProviderRead,
    AcmeProviderTestRead,
    ApplyIn,
    AuditActionRead,
    AuditEventRead,
    AuthProviders,
    BreakGlassConfirmIn,
    BreakGlassEnrollIn,
    BreakGlassEnrollment,
    BreakGlassIn,
    BreakGlassRead,
    BreakGlassSwitchIn,
    CaImportIn,
    CertImportIn,
    CertIssueIn,
    CertReplaceIn,
    ChangeSetDiff,
    ChangeSetRead,
    ChangeSetSummary,
    DeployIn,
    DiscoveryGroup,
    DnsNameMatchRead,
    DnsProviderIn,
    DnsProviderPatch,
    DnsProviderRead,
    DnsProviderTestIn,
    DnsProviderTestRead,
    DnsProviderTypeRead,
    EngineIn,
    EngineRead,
    EngineRenderedRead,
    EngineStateOut,
    EngineStatusRead,
    EnrollIn,
    EnrollmentToken,
    EnrollOut,
    Health,
    IdentityProviderIn,
    IdentityProviderPatch,
    IdentityProviderRead,
    IdpTestIn,
    IdpTestResult,
    IdpTestStarted,
    JobAccepted,
    JobRead,
    Me,
    MemberStateIn,
    MemberStateResult,
    MetricsOverview,
    MetricsOverviewItem,
    MetricsRead,
    MonitorTestIn,
    MonitorTestOut,
    NextFree,
    OpenIn,
    OpsIn,
    Page,
    PermissionMatrix,
    PlacementRead,
    PrincipalNameRead,
    ProblemOut,
    ProtocolRulesRead,
    ReasonIn,
    RevisionRead,
    RevisionSummary,
    RoleMappingIn,
    RoleMappingPatch,
    RoleMappingRead,
    RunningConfigRead,
    SdnApplyRead,
    ServiceEngineRead,
    SessionsRevoked,
    StateOut,
    SystemRoleMappingIn,
    SystemSettingsIn,
    SystemSettingsRead,
    SystemTokenCreated,
    SystemTokenIn,
    SystemTokenRead,
    TenantIn,
    TenantPatch,
    TenantRead,
    TokenCreated,
    TokenIn,
    TokenRead,
    UserDetail,
    UserRead,
    VipAllocation,
    VipPoolUsage,
    VirtualServiceStatusRead,
)
from switchyard.api.stream_models import EVENTS
from switchyard.db import audit_log
from switchyard.db.audit_actions import CATALOG
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import permissions
from switchyard.domain.changes import (
    COLLECTION,
    Action,
    ChangeOp,
    DraftStatus,
    ObjectType,
)
from switchyard.domain.field_rules import protocol_rules
from switchyard.domain.impact import impact as impact_of
from switchyard.domain.models import AcmeChallenge, CertSource, Protocol
from switchyard.domain.workflow import ChangeSetState, EngineState, JobState
from switchyard.ids import uuid7
from switchyard.pipeline.engines import EngineDirectory
from switchyard.render.escape import RenderError
from switchyard.render.fragments import RENDERED_TYPES, rendered_fragments
from switchyard.services import principals
from switchyard.services.acme_providers import AcmeProviders
from switchyard.services.break_glass import BreakGlassService, BreakGlassStatus
from switchyard.services.certificates import CertificateJobs, CertificateService
from switchyard.services.changesets import (
    ChangeSetDetail,
    ChangeSetService,
    check_ops_allowed,
    require,
)
from switchyard.services.context import Databases, Principal
from switchyard.services.dns_providers import DnsProviders
from switchyard.services.engines import EngineDetail, EngineService
from switchyard.services.enrollment import EnrollmentError, EnrollmentService
from switchyard.services.hosts import HostService
from switchyard.services.identity_providers import (
    IdentityProviderRecord,
    IdentityProviderService,
)
from switchyard.services.infra import DeployRequest, InfraJobs
from switchyard.services.jobs import JobService
from switchyard.services.members import MemberService
from switchyard.services.probes import MonitorTestService
from switchyard.services.role_mappings import RoleMappingRecord, RoleMappingService
from switchyard.services.sessions import (
    LoginError,
    LoginResult,
    SessionInfo,
    SessionService,
    UserDisabledError,
)
from switchyard.services.settings import LXC_DISABLED, SystemSettings
from switchyard.services.status import (
    EngineStatus,
    StatusService,
    VirtualServiceStatus,
    virtual_service_states,
)
from switchyard.services.tenants import TenantRecord, TenantService
from switchyard.services.throttle import source_rule
from switchyard.services.tokens import TokenService
from switchyard.services.users import UserRecord, UserService

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


@dataclass
class ApiServices:
    """Everything the routes call."""

    db: Databases
    tenants: TenantService
    tokens: TokenService
    change_sets: ChangeSetService
    engines: EngineService
    members: MemberService
    status: StatusService
    infra: InfraJobs
    cert_jobs: CertificateJobs
    jobs: JobService
    role_mappings: RoleMappingService
    users: UserService
    certs: CertificateService | None = None
    telemetry: Telemetry | None = None
    monitor_tests: MonitorTestService = None  # type: ignore[assignment]
    enrollment: EnrollmentService | None = None
    sessions: SessionService | None = None
    identity_providers: IdentityProviderService | None = None
    break_glass: BreakGlassService | None = None
    hosts: HostService | None = None
    settings: SystemSettings = None  # type: ignore[assignment]
    acme_providers: AcmeProviders | None = None
    dns_providers: DnsProviders | None = None


def build_services(
    db: Databases,
    directory: EngineDirectory,
    secrets: SecretsReader | None = None,
    certs: CertificateService | None = None,
    enrollment: EnrollmentService | None = None,
    sessions: SessionService | None = None,
    telemetry: Telemetry | None = None,
    *,
    identity_providers: IdentityProviderService | None = None,
    break_glass: BreakGlassService | None = None,
    proxmox: Proxmox | None = None,
) -> ApiServices:
    """Wire the services over one set of database handles.

    Identity-provider and break-glass administration are built from ``sessions`` (which owns the
    OIDC client and the forced-break-glass flag) and ``secrets`` unless passed in.
    """
    writer = cast("SecretsWriter | None", secrets if hasattr(secrets, "put") else None)
    if sessions is not None and identity_providers is None:
        identity_providers = IdentityProviderService(
            db,
            sessions.oidc,
            secrets,
            writer,
        )
    if sessions is not None and break_glass is None:
        break_glass = BreakGlassService(
            db,
            secrets,
            writer,
            forced=sessions.break_glass_enabled,
        )
    engines = EngineService(db)
    return ApiServices(
        db=db,
        tenants=TenantService(db),
        tokens=TokenService(db),
        change_sets=ChangeSetService(db),
        engines=engines,
        members=MemberService(db, directory),
        status=StatusService(db, directory, secrets),
        infra=InfraJobs(db),
        cert_jobs=CertificateJobs(db),
        jobs=JobService(db),
        role_mappings=RoleMappingService(db),
        users=UserService(db),
        certs=certs,
        enrollment=enrollment,
        sessions=sessions,
        telemetry=telemetry,
        monitor_tests=MonitorTestService(db, directory),
        identity_providers=identity_providers,
        break_glass=break_glass,
        hosts=HostService(db, engines, proxmox),
        settings=SystemSettings(db),
        acme_providers=AcmeProviders(db, secrets) if writer is not None else None,
        dns_providers=DnsProviders(db, secrets) if writer is not None else None,
    )


# -- dependencies ---------------------------------------------------------------------------------


def _services(request: Request) -> ApiServices:
    services: ApiServices = request.app.state.services
    return services


Services = Annotated[ApiServices, Depends(_services)]


SESSION_COOKIE = "sy_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def scrape_access(
    request: Request, services: Services, authorization: Annotated[str | None, Header()] = None
) -> None:
    """Allow the metrics scrape token, or a system administrator.

    The scrape token is ``SWITCHYARD_METRICS_TOKEN``: it can read ``/metrics`` and the Prometheus
    service-discovery list and nothing else, so Prometheus needs no administrator account.
    """
    token: str | None = request.app.state.metrics_token
    if (
        token
        and authorization
        and authorization.lower().startswith("bearer ")
        and hmac.compare_digest(authorization[7:].strip().encode(), token.encode())
    ):
        return
    require(await current_identity(request, services, authorization), "manage", "tenant")


Scrape = Depends(scrape_access)


async def current_identity(
    request: Request, services: Services, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    """Authenticate the request: a bearer API token, or the session cookie plus a CSRF header.

    Bearer tokens are for machines and are not subject to CSRF. A cookie session must send
    ``X-CSRF-Token`` (the value from ``GET /me``) on every request that changes something.
    Failures carry a stable ``code``: ``invalid_token``, ``session_expired``, ``csrf``,
    ``user_disabled`` or ``unauthenticated``.
    """
    headers = {"WWW-Authenticate": "Bearer"}
    if authorization and authorization.lower().startswith("bearer "):
        principal = await services.tokens.authenticate(authorization[7:].strip())
        if principal is None:
            raise ApiError(
                401, "invalid, expired or revoked token", headers=headers, code="invalid_token"
            )
        return principal
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and services.sessions is not None:
        try:
            info = await services.sessions.authenticate(cookie)
        except UserDisabledError:
            raise ApiError(
                403, "your account has been disabled by an administrator", code="user_disabled"
            ) from None
        if info is None:
            raise ApiError(
                401,
                "your session has expired; log in again",
                headers=headers,
                code="session_expired",
            )
        if request.method not in SAFE_METHODS and not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), info.csrf_token
        ):
            raise ApiError(403, "missing or wrong X-CSRF-Token header", code="csrf")
        request.state.session = info
        return info.principal()
    raise ApiError(
        401,
        "send an API token as 'Authorization: Bearer <token>' or log in",
        headers=headers,
        code="unauthenticated",
    )


FullWho = Annotated[Principal, Depends(current_identity)]


async def current_principal(request: Request, who: FullWho, services: Services) -> Principal:
    """The authenticated principal, narrowed to the tenant in the path (``{t}``) if there is one.

    A person can hold different roles in different tenants. Services check ``principal.role``, so
    inside a tenant route it must be the role held *in that tenant*.
    """
    t = request.path_params.get("t")
    if t is None or not who.tenant_roles:
        return who
    record = await services.tenants.resolve(who, t)
    return who.for_tenant(record.id)


Who = Annotated[Principal, Depends(current_principal)]


async def current_tenant(t: str, who: Who, services: Services) -> TenantRecord:
    """Resolve the ``{t}`` path segment (name or id) and check access."""
    return await services.tenants.resolve(who, t)


Tenant = Annotated[TenantRecord, Depends(current_tenant)]


def _rid() -> str | None:
    return request_id_var.get()


def _etag(version: int) -> str:
    return f'"{version}"'


def _mapping_json(r: RoleMappingRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "provider_id": str(r.provider_id),
        "provider": r.provider,
        "group_name": r.group_name,
        "label": r.label,
        "tenant_id": str(r.tenant_id) if r.tenant_id else None,
        "tenant": r.tenant,
        "role": r.role.value,
        "version": r.version,
        "created_by": r.created_by,
        "created_at": r.created_at,
    }


def _user_json(r: UserRecord, *, detail: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(r.id),
        "provider_id": str(r.provider_id),
        "provider": r.provider,
        "username": r.username,
        "email": r.email,
        "groups": list(r.groups),
        "first_login_at": r.first_login_at,
        "last_login_at": r.last_login_at,
        "last_seen_at": r.last_seen_at,
        "disabled_at": r.disabled_at,
        "grants": [
            {
                "tenant_id": str(g.tenant_id) if g.tenant_id else None,
                "tenant": g.tenant,
                "role": g.role.value,
                "via": [{"provider": v.provider, "group": v.group} for v in g.via],
            }
            for g in r.grants
        ],
    }
    if detail:
        out |= {"subject": r.subject, "active_sessions": r.active_sessions}
    return out


def _if_match(value: str | None) -> int:
    if value is None:
        raise ApiError(428, "send If-Match with the object's ETag")
    try:
        return int(value.strip().strip('"'))
    except ValueError:
        raise ApiError(400, "If-Match must be an ETag from this API") from None


# -- the generic object routes --------------------------------------------------------------------

COLLECTIONS: dict[str, ObjectType] = {
    "networks": ObjectType.NETWORK,
    "vip-pools": ObjectType.VIP_POOL,
    "se-groups": ObjectType.SE_GROUP,
    "virtual-services": ObjectType.VIRTUAL_SERVICE,
    "pools": ObjectType.POOL,
    "health-monitors": ObjectType.HEALTH_MONITOR,
    "certificates": ObjectType.CERTIFICATE,
    "policy-sets": ObjectType.POLICY_SET,
    "routing-profiles": ObjectType.ROUTING_PROFILE,
}


def _ok(
    example: Any,
    code: int = 200,
    description: str = "Success",
    *,
    headers: dict[str, Any] | None = None,
    extra: dict[int | str, dict[str, Any]] | None = None,
    problems: bool = True,
) -> dict[int | str, dict[str, Any]]:
    """The ``responses=`` entry of a route: a documented example plus the shared error shapes."""
    entry: dict[str, Any] = {
        "description": description,
        "content": {"application/json": {"example": example}},
    }
    if headers:
        entry["headers"] = headers
    out: dict[int | str, dict[str, Any]] = {code: entry}
    if problems:
        out |= PROBLEM_RESPONSES
    return out | (extra or {})


def _engine_json(d: EngineDetail, online: dict[str, bool]) -> dict[str, Any]:
    e = d.record
    return {
        "id": str(e.id),
        "name": e.name,
        "url": e.dataplane_url,
        "state": e.state.value,
        "se_group_id": str(e.se_group_id),
        "mgmt_ip": e.mgmt_ip,
        "agent_url": e.agent_url,
        "pve_node": d.pve_node,
        "vmid": d.vmid,
        "guest_kind": d.guest_kind,
        "addresses": [{"network_id": n, "address": a} for n, a in d.data_ips],
        "agent_enrolled": d.agent_cert_serial is not None,
        "agent_cert_serial": d.agent_cert_serial,
        "host_online": online.get(d.pve_node) if d.pve_node else None,
    }


def _object_json(obj: Any, status: DraftStatus, changed: tuple[str, ...]) -> dict[str, Any]:
    data: dict[str, Any] = obj.model_dump(mode="json")
    data["draft_status"] = status.value
    data["changed_fields"] = list(changed)
    return data


async def _view(
    services: ApiServices,
    who: Principal,
    tenant: TenantRecord,
    object_type: ObjectType,
    change_set: UUID | None,
) -> list[dict[str, Any]]:
    from switchyard.db.repository import load_snapshot

    attr = COLLECTION[object_type]
    if change_set is None:
        async with tenant_session(services.db.app, [tenant.id]) as session:
            base = await load_snapshot(session)
        return [_object_json(o, DraftStatus.UNCHANGED, ()) for o in getattr(base, attr)]
    base, draft = await services.change_sets.draft_view(who, tenant.id, change_set)
    by_id = {(o.object_type, o.object_id): o for o in draft.objects}
    out = []
    for obj in getattr(draft.snapshot, attr):
        info = by_id.get((object_type, obj.id))
        out.append(
            _object_json(
                obj,
                info.status if info else DraftStatus.UNCHANGED,
                info.changed_fields if info else (),
            )
        )
    for info in draft.objects:
        if info.object_type is object_type and info.status is DraftStatus.DELETED:
            old = next(o for o in getattr(base, attr) if o.id == info.object_id)
            out.append(_object_json(old, DraftStatus.DELETED, ()))
    return out


AUDIT_DESCRIPTION = (
    "Append-only record of who did what. Filter by `actor` and `action` (exact, or a prefix when "
    "the value ends in `*`, for example `change_set.*`; the names are in `GET /audit-actions`), "
    "by `object` (exact reference such as `change_set:<id>` or `job:<id>`), by time (`since` "
    "inclusive, `until` exclusive) and by free text `q` (substring of actor, action or object). "
    "Newest first; page with `limit` (1-500) and `cursor`."
)


@dataclass
class AuditFilters:
    """Query parameters shared by the audit routes."""

    actor: Annotated[str | None, Query()] = None
    action: Annotated[str | None, Query()] = None
    object: Annotated[str | None, Query()] = None
    since: Annotated[datetime | None, Query()] = None
    until: Annotated[datetime | None, Query()] = None
    q: Annotated[str | None, Query(max_length=200)] = None
    cursor: Annotated[str | None, Query()] = None
    limit: Annotated[int, Query(ge=1, le=500)] = 100
    tenant_id: UUID | None = field(default=None, init=False)


async def _audit_page(session: Any, f: AuditFilters) -> Page[Any]:
    from sqlalchemy import and_, or_, select

    from switchyard.db.ops_models import AuditEventRow as A

    def matches(column: Any, value: str) -> Any:
        if value.endswith("*"):
            return column.like(_like_escape(value[:-1]) + "%", escape="\\")
        return column == value

    stmt = select(A).order_by(A.at.desc(), A.id.desc()).limit(f.limit + 1)
    if f.tenant_id:
        stmt = stmt.where(A.tenant_id == f.tenant_id)
    if f.actor:
        stmt = stmt.where(matches(A.actor, f.actor))
    if f.action:
        stmt = stmt.where(matches(A.action, f.action))
    if f.object:
        stmt = stmt.where(matches(A.object_ref, f.object))
    if f.since:
        stmt = stmt.where(A.at >= f.since)
    if f.until:
        stmt = stmt.where(A.at < f.until)
    if f.q:
        like = "%" + _like_escape(f.q) + "%"
        stmt = stmt.where(
            or_(
                A.actor.ilike(like, escape="\\"),
                A.action.ilike(like, escape="\\"),
                A.object_ref.ilike(like, escape="\\"),
            )
        )
    if f.cursor:
        try:
            at_text, id_text = f.cursor.split("|", 1)
            after = (_EPOCH + timedelta(microseconds=int(at_text)), UUID(id_text))
        except ValueError:
            raise ApiError(400, "cursor is not valid") from None
        stmt = stmt.where(or_(A.at < after[0], and_(A.at == after[0], A.id < after[1])))
    rows = list((await session.execute(stmt)).scalars())
    window = rows[: f.limit]
    return Page(
        items=[
            AuditEventRead(
                at=r.at,
                tenant_id=r.tenant_id,
                actor=r.actor,
                action=r.action,
                object=r.object_ref,
                diff=r.diff,
                request_id=r.request_id,
            )
            for r in window
        ],
        next_cursor=(
            f"{(window[-1].at - _EPOCH) // timedelta(microseconds=1)}|{window[-1].id}"
            if len(rows) > f.limit
            else None
        ),
    )


GENERIC_ENGINE_ERROR = (
    "The controller could not reach or talk to this engine; ask an administrator."
)
EXPORT_FORMATS = {"csv": "text/csv", "jsonl": "application/x-ndjson"}
EXPORT_PAGE = 500
EXPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The events as a file.",
        "content": {
            "text/csv": {
                "example": "at,tenant_id,actor,action,object,request_id,diff\n"
                "2026-10-11T08:30:00Z,,user:alice,change_set.apply,change_set:1,r-1,\n"
            },
            "application/x-ndjson": {
                "example": '{"action": "change_set.apply", "actor": "user:alice", '
                '"at": "2026-10-11T08:30:00Z", "diff": null, "object": "change_set:1", '
                '"request_id": "r-1", "tenant_id": null}\n'
            },
        },
    }
}
CSV_COLUMNS = ("at", "tenant_id", "actor", "action", "object", "request_id", "diff")


def _csv_safe(value: str) -> str:
    r"""Defuse spreadsheet formulas: a cell starting with = + - @ or a control character is quoted.

    Example:
        >>> _csv_safe("=HYPERLINK(\"http://x\")")
        '\'=HYPERLINK("http://x")'
        >>> _csv_safe("user:alice")
        'user:alice'
    """
    return "'" + value if value[:1] in {"=", "+", "-", "@", "\t", "\r"} else value


async def _audit_export(
    open_session: Callable[[], Any], f: AuditFilters, fmt: str
) -> AsyncIterator[str]:
    """Stream every audit event matching ``f``, newest first, in pages of ``EXPORT_PAGE``.

    Each page runs in its own short transaction, so a long export holds no connection open.
    """
    import copy
    import csv
    import io
    import json

    page_filters = copy.copy(f)
    page_filters.limit = EXPORT_PAGE
    if fmt == "csv":
        out = io.StringIO()
        csv.writer(out).writerow(CSV_COLUMNS)
        yield out.getvalue()
    while True:
        async with open_session() as session:
            page = await _audit_page(session, page_filters)
        out = io.StringIO()
        writer = csv.writer(out)
        for event in page.items:
            data = event.model_dump(mode="json")
            if fmt == "csv":
                data["diff"] = json.dumps(data["diff"], sort_keys=True) if data["diff"] else ""
                writer.writerow(
                    ["" if data[c] is None else _csv_safe(str(data[c])) for c in CSV_COLUMNS]
                )
            else:
                out.write(json.dumps(data, sort_keys=True) + "\n")
        if out.tell():
            yield out.getvalue()
        if page.next_cursor is None:
            return
        page_filters.cursor = page.next_cursor


def _export_response(
    open_session: Callable[[], Any], f: AuditFilters, fmt: str, name: str
) -> StreamingResponse:
    if fmt not in EXPORT_FORMATS:
        raise ApiError(422, f"format must be one of: {', '.join(sorted(EXPORT_FORMATS))}")
    f.cursor = None
    return StreamingResponse(
        _audit_export(open_session, f, fmt),
        media_type=EXPORT_FORMATS[fmt] + "; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'},
    )


def _export_diff(f: AuditFilters, fmt: str) -> dict[str, Any]:
    """The filters of an export, for its audit event."""
    fields = ("actor", "action", "object", "since", "until", "q")
    return {"format": fmt} | {k: str(getattr(f, k)) for k in fields if getattr(f, k)}


async def _pool_usage(
    services: ApiServices,
    who: Principal,
    tenant: TenantRecord,
    pool_id: UUID,
    change_set: UUID | None,
) -> VipPoolUsage:
    """Usage of one VIP pool, live or as a draft would leave it."""
    from switchyard.db.repository import load_snapshot
    from switchyard.domain import ipam

    statuses: dict[UUID, DraftStatus] = {}
    if change_set is None:
        async with tenant_session(services.db.app, [tenant.id]) as session:
            snapshot = await load_snapshot(session)
    else:
        _, draft = await services.change_sets.draft_view(who, tenant.id, change_set)
        snapshot = draft.snapshot
        statuses = {
            o.object_id: o.status
            for o in draft.objects
            if o.object_type is ObjectType.VIRTUAL_SERVICE
        }
    pool = next((p for p in snapshot.vip_pools if p.id == pool_id), None)
    network = next((n for n in snapshot.networks if pool and n.id == pool.network_id), None)
    if pool is None or network is None:
        raise ApiError(404, f"vip-pools {pool_id} not found")
    addresses = ipam.pool_addresses(pool, network)
    inside = set(addresses)
    held = sorted(
        (v for v in snapshot.virtual_services if v.vip in inside),
        key=lambda v: (int(v.vip), v.name),
    )
    from sqlalchemy import select

    from switchyard.db.ops_models import ServiceEngineRow
    from switchyard.services.infra import ipam_address

    async with tenant_session(services.db.app, [tenant.id]) as session:
        engine_rows = (await session.execute(select(ServiceEngineRow))).scalars().all()
    held_by_engines = {
        ipam_address(a)
        for r in engine_rows
        for a in [r.mgmt_ip, *[e["address"] for e in r.data_ips]]
        if a
    } & inside
    taken = {v.vip for v in held} | held_by_engines
    try:
        next_free: str | None = str(ipam.allocate(pool, network, taken))
    except ipam.PoolExhaustedError:
        next_free = None
    return VipPoolUsage(
        pool_id=pool.id,
        total=len(addresses),
        used=len(taken),
        free=len(addresses) - len(taken),
        engine_addresses=[str(a) for a in sorted(held_by_engines, key=int)],
        allocations=[
            VipAllocation(
                address=str(v.vip),
                virtual_service_id=v.id,
                virtual_service_name=v.name,
                enabled=v.enabled,
                draft_status=statuses.get(v.id, DraftStatus.UNCHANGED),
            )
            for v in held
        ],
        next_free=next_free,
    )


@dataclass
class ListFilters:
    """Filters every object list accepts."""

    name: Annotated[str | None, Query(description="Exactly this name.")] = None
    q: Annotated[
        str | None, Query(description="Substring of the name, case-insensitive.", max_length=200)
    ] = None
    sort: Annotated[
        Literal["name", "-name"], Query(description="Order by name, ascending or descending.")
    ] = "name"


@dataclass
class VirtualServiceFilters(ListFilters):
    """Extra filters of virtual services."""

    se_group: Annotated[UUID | None, Query(description="Served by this SE group.")] = None
    protocol: Annotated[Protocol | None, Query(description="`http`, `tcp` or `udp`.")] = None
    enabled: Annotated[bool | None, Query(description="Only enabled or only disabled.")] = None
    pool: Annotated[UUID | None, Query(description="Uses this pool (default or in a route).")] = (
        None
    )
    certificate: Annotated[UUID | None, Query(description="Uses this certificate.")] = None


@dataclass
class PoolFilters(ListFilters):
    """Extra filters of pools."""

    health_monitor: Annotated[UUID | None, Query(description="Checked by this monitor.")] = None


@dataclass
class KindFilters(ListFilters):
    """Extra filter of the collections that are unions on ``kind``."""

    kind: Annotated[str | None, Query(description="Only this `kind`.")] = None


@dataclass
class SeGroupFilters(ListFilters):
    """Extra filter of SE groups."""

    ha_mode: Annotated[str | None, Query(description="`bgp` or `vrrp`.")] = None


@dataclass
class CertificateFilters(ListFilters):
    """Extra filters of certificates."""

    source: Annotated[CertSource | None, Query(description="How it was obtained.")] = None
    expires_within_days: Annotated[
        int | None, Query(ge=0, le=3650, description="Expires within this many days (or already).")
    ] = None


FILTERS: dict[str, type[ListFilters]] = {
    "virtual-services": VirtualServiceFilters,
    "pools": PoolFilters,
    "policy-sets": KindFilters,
    "routing-profiles": KindFilters,
    "se-groups": SeGroupFilters,
    "certificates": CertificateFilters,
}


def _filtered(items: list[dict[str, Any]], f: ListFilters) -> list[dict[str, Any]]:
    """Apply the list filters to object dicts (JSON form)."""
    out = items
    if f.name:
        out = [o for o in out if o["name"] == f.name]
    if f.q:
        needle = f.q.lower()
        out = [o for o in out if needle in o["name"].lower()]
    if isinstance(f, VirtualServiceFilters):
        if f.se_group:
            out = [o for o in out if o["se_group_id"] == str(f.se_group)]
        if f.protocol:
            out = [o for o in out if o["protocol"] == f.protocol.value]
        if f.enabled is not None:
            out = [o for o in out if o["enabled"] is f.enabled]
        if f.pool:
            want = str(f.pool)
            out = [
                o
                for o in out
                if o.get("default_pool_id") == want
                or any(r.get("pool_id") == want for r in o.get("routes", []))
            ]
        if f.certificate:
            out = [o for o in out if str(f.certificate) in o.get("certificate_ids", [])]
    if isinstance(f, PoolFilters) and f.health_monitor:
        out = [o for o in out if o.get("health_monitor_id") == str(f.health_monitor)]
    if isinstance(f, KindFilters) and f.kind:
        out = [o for o in out if o.get("kind") == f.kind]
    if isinstance(f, SeGroupFilters) and f.ha_mode:
        out = [o for o in out if o.get("ha_mode") == f.ha_mode]
    if isinstance(f, CertificateFilters):
        if f.source:
            out = [o for o in out if o.get("source") == f.source.value]
        if f.expires_within_days is not None:
            limit_at = datetime.now(UTC) + timedelta(days=f.expires_within_days)
            out = [
                o
                for o in out
                if datetime.fromisoformat(str(o["not_after"]).replace("Z", "+00:00")) <= limit_at
            ]
    return out


def _page(
    items: list[dict[str, Any]], cursor: str | None, limit: int, descending: bool = False
) -> Page[Any]:
    items = sorted(items, key=lambda o: (o["name"], o["id"]), reverse=descending)
    if cursor:
        after = tuple(cursor.split("|", 1))
        items = [
            o
            for o in items
            if ((o["name"], o["id"]) < after if descending else (o["name"], o["id"]) > after)
        ]
    window = items[:limit]
    nxt = f"{window[-1]['name']}|{window[-1]['id']}" if len(items) > limit else None
    return Page(items=window, next_cursor=nxt)


async def _write(
    services: ApiServices,
    who: Principal,
    tenant: TenantRecord,
    op: ChangeOp,
    change_set: UUID | None,
    apply: bool,
    summary: str,
) -> Accepted:
    check_ops_allowed(who, [op])  # the role decides before the shape of the request
    if change_set is None and not apply:
        raise ApiError(400, "pass ?change_set=<id> to add to a draft, or ?apply=true to apply now")
    if change_set is not None:
        detail = await services.change_sets.add_ops(
            who, tenant.id, change_set, [op], request_id=_rid()
        )
        return Accepted(
            change_set_id=change_set, object_id=op.object_id, op_index=len(detail.ops) - 1
        )
    opened = await services.change_sets.open(who, tenant.id, summary, request_id=_rid())
    try:
        await services.change_sets.add_ops(
            who, tenant.id, opened.record.id, [op], request_id=_rid()
        )
        job = await services.change_sets.apply(who, tenant.id, opened.record.id, request_id=_rid())
    except Exception:
        await services.change_sets.discard(who, tenant.id, opened.record.id, request_id=_rid())
        raise
    return Accepted(change_set_id=opened.record.id, object_id=op.object_id, job_id=job)


def _object_routes(router: APIRouter, slug: str, object_type: ObjectType) -> None:
    api = OBJECT_APIS[object_type]
    base = f"/tenants/{{t}}/{slug}"
    tag = slug
    noun = slug.replace("-", " ")
    sample = ex.OBJECTS[slug]
    filter_type = FILTERS.get(slug, ListFilters)
    create_in: Any = api.create
    patch_in: Any = api.patch
    read_out: Any = api.read

    @router.get(
        base,
        tags=[tag],
        name=f"list_{slug}",
        summary=f"List {noun}",
        response_model=Page[read_out],
        responses=_ok({"items": [sample], "next_cursor": None}),
    )
    async def list_objects(
        tenant: Tenant,
        who: Who,
        services: Services,
        filters: Annotated[Any, Depends(filter_type)],
        change_set: UUID | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Page[Any]:
        """List the tenant's objects of this type, by name; with ``change_set`` the draft view."""
        items = _filtered(await _view(services, who, tenant, object_type, change_set), filters)
        return _page(items, cursor, limit, descending=filters.sort == "-name")

    @router.get(
        base + "/{object_id}",
        tags=[tag],
        name=f"get_{slug}",
        summary=f"Get one of the {noun}",
        response_model=read_out,
        responses=_ok(sample, headers=ETAG_HEADER),
    )
    async def get_object(
        object_id: UUID,
        tenant: Tenant,
        who: Who,
        services: Services,
        response: Response,
        change_set: UUID | None = None,
    ) -> dict[str, Any]:
        """Return one object. The ``ETag`` header carries its version for ``If-Match``."""
        for item in await _view(services, who, tenant, object_type, change_set):
            if item["id"] == str(object_id):
                response.headers["ETag"] = _etag(item["version"])
                return item
        raise ApiError(404, f"{slug} {object_id} not found")

    @router.post(
        base,
        tags=[tag],
        status_code=202,
        name=f"create_{slug}",
        summary=f"Create one of the {noun}",
        response_model=Accepted,
        responses=_ok(ex.ACCEPTED, 202, "Recorded in the change set or queued.")
        | VALIDATION_RESPONSE,
    )
    async def create_object(
        body: create_in,
        tenant: Tenant,
        who: Who,
        services: Services,
        change_set: UUID | None = None,
        apply: bool = False,
        object_id: UUID | None = None,
    ) -> Accepted:
        """Add a create operation to ``change_set``, or apply it at once with ``apply=true``."""
        data = body.model_dump(mode="json", exclude_unset=True)
        op = ChangeOp(
            object_type=object_type, object_id=object_id or uuid7(), action=Action.CREATE, body=data
        )
        return await _write(
            services, who, tenant, op, change_set, apply, f"create {slug}/{data.get('name')}"
        )

    @router.patch(
        base + "/{object_id}",
        tags=[tag],
        status_code=202,
        name=f"update_{slug}",
        summary=f"Change one of the {noun}",
        response_model=Accepted,
        responses=_ok(ex.ACCEPTED, 202, "Recorded in the change set or queued.")
        | IF_MATCH_RESPONSES
        | VALIDATION_RESPONSE,
    )
    async def update_object(
        object_id: UUID,
        body: patch_in,
        tenant: Tenant,
        who: Who,
        services: Services,
        change_set: UUID | None = None,
        apply: bool = False,
        if_match: Annotated[str | None, Header()] = None,
    ) -> Accepted:
        """Add an update operation (only the fields sent change). Requires ``If-Match``."""
        data = body.model_dump(mode="json", exclude_unset=True)
        op = ChangeOp(
            object_type=object_type,
            object_id=object_id,
            action=Action.UPDATE,
            body=data,
            expected_version=_if_match(if_match),
        )
        return await _write(
            services, who, tenant, op, change_set, apply, f"update {slug}/{object_id}"
        )

    @router.delete(
        base + "/{object_id}",
        tags=[tag],
        status_code=202,
        name=f"delete_{slug}",
        summary=f"Delete one of the {noun}",
        response_model=Accepted,
        responses=_ok(ex.ACCEPTED, 202, "Recorded in the change set or queued.")
        | IF_MATCH_RESPONSES,
    )
    async def delete_object(
        object_id: UUID,
        tenant: Tenant,
        who: Who,
        services: Services,
        change_set: UUID | None = None,
        apply: bool = False,
        if_match: Annotated[str | None, Header()] = None,
    ) -> Accepted:
        """Add a delete operation. Requires ``If-Match``."""
        op = ChangeOp(
            object_type=object_type,
            object_id=object_id,
            action=Action.DELETE,
            expected_version=_if_match(if_match),
        )
        return await _write(
            services, who, tenant, op, change_set, apply, f"delete {slug}/{object_id}"
        )


def _rendered_route(router: APIRouter, slug: str, object_type: ObjectType) -> None:
    """``GET /tenants/{t}/<slug>/{id}/rendered``: the config text one object produced."""

    @router.get(
        f"/tenants/{{t}}/{slug}/{{object_id}}/rendered",
        tags=[slug],
        name=f"rendered_{slug}",
        summary=f"Rendered config of a {slug.rstrip('s').replace('-', ' ')}",
        response_model=RenderedConfig,
        responses=_ok(ex.RENDERED),
    )
    async def rendered(
        object_id: UUID,
        tenant: Tenant,
        who: Who,
        services: Services,
        change_set: UUID | None = None,
    ) -> RenderedConfig:
        from switchyard.db.repository import load_snapshot

        if change_set is None:
            async with tenant_session(services.db.app, [tenant.id]) as session:
                snapshot = await load_snapshot(session)
        else:
            _, draft = await services.change_sets.draft_view(who, tenant.id, change_set)
            snapshot = draft.snapshot
        if not any(o.id == object_id for o in getattr(snapshot, COLLECTION[object_type])):
            raise ApiError(404, f"{slug} {object_id} not found")
        try:
            fragments = rendered_fragments(snapshot, object_type, object_id)
        except RenderError as exc:
            raise ApiError(422, f"cannot render: {exc}") from exc
        return RenderedConfig(items=[RenderedFragment(**f.__dict__) for f in fragments])


# -- application ----------------------------------------------------------------------------------


def _detail_json(detail: ChangeSetDetail) -> dict[str, Any]:
    r = detail.record
    return {
        "id": str(r.id),
        "state": r.state.value,
        "author": r.author,
        "summary": r.summary,
        "base_revision": r.base_revision,
        "approver": r.approver,
        "reject_comment": r.reject_comment,
        "error": r.error,
        "created_at": r.created_at,
        "ops": [o.model_dump(mode="json") for o in detail.ops],
    }


def _problem_json_openapi(app: FastAPI) -> dict[str, Any]:
    """OpenAPI document where every error response is ``application/problem+json``.

    FastAPI documents errors as ``application/json`` and its own 422 shape; this API answers with
    RFC 9457 problems (see :mod:`switchyard.api.problems`), so the document says so.
    """
    if app.openapi_schema is not None:
        return app.openapi_schema
    schema = FastAPI.openapi(app)
    problem_ref = {"$ref": "#/components/schemas/ProblemOut"}
    for item in schema["paths"].values():
        for op in item.values():
            for code, response in op.get("responses", {}).items():
                if not code.startswith(("4", "5")):
                    continue
                content = response.setdefault("content", {})
                content.pop("application/json", None)
                content["application/problem+json"] = {"schema": problem_ref}
    for unused in ("HTTPValidationError", "ValidationError"):
        schema["components"]["schemas"].pop(unused, None)
    docs_meta.enrich(schema)
    app.openapi_schema = schema
    return schema


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _uuid_cursor(cursor: str | None) -> UUID | None:
    if cursor is None:
        return None
    try:
        return UUID(cursor)
    except ValueError:
        raise ApiError(400, "cursor is not one issued by this API") from None


def _int_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return None
    try:
        return int(cursor)
    except ValueError:
        raise ApiError(400, "cursor is not one issued by this API") from None


def create_app(
    services: ApiServices,
    *,
    public_url: str | None = None,
    metrics_token: str | None = None,
    grafana_url: str | None = None,
) -> FastAPI:
    """Build the application over ready-made services.

    Args:
        services: The wired services.
        public_url: External base URL (``https://switchyard.example.test``), used to build login
            redirect URIs and to decide whether cookies are ``Secure``.
        grafana_url: Base URL of Grafana, shown to the UI as ``links.grafana`` on ``GET /me``.
        metrics_token: Bearer token that may read ``/metrics`` and the Prometheus
            service-discovery list; without it only system administrators can.
    """
    app = FastAPI(
        title="Switchyard API",
        version="1.0.0",
        description=(
            "Multi-tenant load balancer controller. Every write goes through a change set; "
            "errors are `application/problem+json`; objects carry an `ETag` that writes send back "
            "as `If-Match`."
        ),
        separate_input_output_schemas=False,
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
    )
    app.state.services = services
    app.state.public_url = public_url
    app.state.metrics_token = metrics_token
    install_handlers(app)

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        try:
            response: Response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        return response

    @app.get("/metrics", include_in_schema=False, dependencies=[Scrape])
    async def metrics() -> Response:
        """The controller's own metrics, Prometheus text format (ADR 0061)."""
        from switchyard.services.observability import collect_metrics, render_exposition

        body = render_exposition(await collect_metrics(services.db))
        return Response(body, media_type="text/plain; version=0.0.4; charset=utf-8")

    router = APIRouter(prefix="/api/v1")

    @router.get(
        "/discovery/prometheus",
        tags=["meta"],
        summary="Prometheus service discovery for the SE exporters",
        description=(
            "Prometheus `http_sd_configs` target groups, one per enabled engine with a management "
            "address, labelled `tenant`, `tenant_name`, `se_group`, `engine` and `vs`. `exporter` "
            "is `haproxy` (default port 8405) or `node` (9100); `port` overrides the port. "
            "Requires `system-admin` or the metrics scrape token."
        ),
        response_model=list[DiscoveryGroup],
        responses=_ok(ex.DISCOVERY),
        dependencies=[Scrape],
    )
    async def prometheus_discovery(
        exporter: Annotated[str, Query(description="`haproxy` or `node`.")] = "haproxy",
        port: Annotated[
            int | None, Query(ge=1, le=65535, description="Exporter port; default per exporter.")
        ] = None,
    ) -> list[dict[str, object]]:
        from switchyard.services.observability import EXPORTERS, discovery_targets

        if exporter not in EXPORTERS:
            raise ApiError(422, f"exporter must be one of: {', '.join(sorted(EXPORTERS))}")
        return await discovery_targets(services.db, exporter=exporter, port=port)

    @router.get(
        "/healthz",
        tags=["meta"],
        summary="Liveness check",
        response_model=Health,
        responses=_ok(ex.HEALTH, problems=False),
    )
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get(
        "/schema/virtual-service",
        tags=["meta"],
        summary="Field rules of a virtual service protocol",
        response_model=ProtocolRulesRead,
        responses=_ok(ex.PROTOCOL_RULES),
    )
    async def protocol_field_rules(who: Who, protocol: Protocol) -> dict[str, Any]:
        """Which service, port and pool fields `protocol` allows (same table as the validator)."""
        rules = protocol_rules(protocol)
        return {
            "protocol": rules.protocol,
            "service_forbidden": list(rules.service_forbidden),
            "port_forbidden": list(rules.port_forbidden),
            "pool_forbidden": list(rules.pool_forbidden),
            "algorithms": list(rules.algorithms),
            "monitor_types": list(rules.monitor_types),
            "member_port_required": rules.member_port_required,
        }

    # login
    def callback_url(request: Request, provider: str) -> str:
        base = request.app.state.public_url or str(request.base_url)
        return f"{base.rstrip('/')}/api/v1/auth/callback/{provider}"

    def set_session_cookie(
        request: Request, response: Response, cookie: str, info: SessionInfo
    ) -> None:
        secure = (request.app.state.public_url or str(request.base_url)).startswith("https")
        response.set_cookie(
            SESSION_COOKIE,
            cookie,
            max_age=int((info.expires_at - datetime.now(UTC)).total_seconds()),
            httponly=True,
            samesite="strict",
            secure=secure,
            path="/",
        )

    def client_source(request: Request) -> str:
        """The address a login attempt comes from, for throttling.

        Behind a reverse proxy set ``SWITCHYARD_TRUST_FORWARDED=1`` to use the last
        ``X-Forwarded-For`` entry (the one the proxy added); otherwise the socket peer is used.
        """
        if os.environ.get("SWITCHYARD_TRUST_FORWARDED") == "1":
            forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
            if forwarded:
                return forwarded
        return request.client.host if request.client else "unknown"

    def login_failed(code: str) -> Response:
        """Send a browser whose sign-in failed to the login page, which explains ``code``."""
        return Response(status_code=302, headers={"Location": f"/login?error={code}"})

    def require_sessions(svc: ApiServices) -> SessionService:
        if svc.sessions is None:
            raise ApiError(503, "login is not configured on this controller")
        return svc.sessions

    @router.get(
        "/auth/providers",
        tags=["auth"],
        summary="List login providers",
        response_model=AuthProviders,
        responses=_ok(ex.AUTH_PROVIDERS, problems=False),
    )
    async def auth_providers(svc: Services) -> dict[str, Any]:
        """Identity providers users can log in with, and whether break-glass login is on."""
        sessions = require_sessions(svc)
        return {
            "providers": [
                {"id": str(p.id), "name": p.name, "protocol": p.protocol}
                for p in await sessions.providers()
            ],
            "break_glass": await sessions.break_glass_on(),
        }

    @router.get(
        "/auth/login/{provider}",
        tags=["auth"],
        status_code=302,
        summary="Start a login",
        response_class=Response,
    )
    async def auth_login(
        provider: str, request: Request, svc: Services, return_to: str | None = None
    ) -> Response:
        """Redirect the browser to the identity provider."""
        sessions = require_sessions(svc)
        try:
            url = await sessions.begin_login(
                provider, redirect_uri=callback_url(request, provider), return_to=return_to
            )
        except LoginError as exc:
            raise ApiError(400, str(exc)) from exc
        return Response(status_code=302, headers={"Location": url})

    @router.get(
        "/auth/callback/{provider}",
        tags=["auth"],
        status_code=302,
        summary="Finish a login",
        response_class=Response,
    )
    async def auth_callback(
        provider: str,
        request: Request,
        svc: Services,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        """Finish the login, set the session cookie and send the browser back to the app."""
        sessions = require_sessions(svc)
        rules = [source_rule(client_source(request))]
        if await sessions.throttle.blocked_for(rules):
            return login_failed("too_many_attempts")
        if error or not code or not state:
            await sessions.throttle.record_failure(rules)
            return login_failed("provider_error")
        try:
            result = await sessions.complete_login(
                state=state, code=code, redirect_uri=callback_url(request, provider)
            )
        except LoginError as exc:
            await sessions.throttle.record_failure(rules)
            return login_failed(exc.code)
        response = Response(status_code=302, headers={"Location": result.return_to})
        if isinstance(result, LoginResult):  # a test sign-in sets no cookie
            set_session_cookie(request, response, result.cookie, result.session)
        return response

    @router.post(
        "/auth/callback/{provider}",
        tags=["auth"],
        status_code=302,
        summary="SAML assertion consumer service",
        response_class=Response,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/x-www-form-urlencoded": {
                        "schema": {
                            "type": "object",
                            "required": ["SAMLResponse", "RelayState"],
                            "properties": {
                                "SAMLResponse": {"type": "string", "description": "Base64."},
                                "RelayState": {"type": "string", "description": "Login state."},
                            },
                        }
                    }
                },
            }
        },
    )
    async def auth_saml_callback(provider: str, request: Request, svc: Services) -> Response:
        """Finish a SAML login. Only answers a login this controller started (SP-initiated).

        The assertion must be signed, answer our request, and not have been used before. On any
        failure the browser is sent to `/login?error=<code>`. No session exists yet, so the CSRF
        header does not apply.
        """
        sessions = require_sessions(svc)
        rules = [source_rule(client_source(request))]
        if await sessions.throttle.blocked_for(rules):
            return login_failed("too_many_attempts")
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        try:
            result = await sessions.complete_saml_login(
                provider=provider,
                saml_response=(form.get("SAMLResponse") or [""])[0],
                relay_state=(form.get("RelayState") or [""])[0],
                acs_url=callback_url(request, provider),
            )
        except LoginError as exc:
            await sessions.throttle.record_failure(rules)
            return login_failed(exc.code)
        response = Response(status_code=302, headers={"Location": result.return_to})
        if isinstance(result, LoginResult):  # a test sign-in sets no cookie
            set_session_cookie(request, response, result.cookie, result.session)
        return response

    @router.get(
        "/auth/saml/{provider}/metadata",
        tags=["auth"],
        summary="SAML service-provider metadata",
        response_class=Response,
        responses={
            200: {
                "description": "SP metadata for the identity provider administrator.",
                "content": {"application/samlmetadata+xml": {"schema": {"type": "string"}}},
            }
        },
    )
    async def auth_saml_metadata(
        provider: str, request: Request, svc: Services, download: bool = False
    ) -> Response:
        """Public, so an identity provider can fetch it by URL. `?download=1` sets a filename."""
        sessions = require_sessions(svc)
        try:
            xml = await sessions.saml_metadata(provider, acs_url=callback_url(request, provider))
        except LoginError as exc:
            raise ApiError(404, str(exc)) from exc
        headers = {"Content-Disposition": f'attachment; filename="{provider}-sp-metadata.xml"'}
        return Response(
            xml,
            media_type="application/samlmetadata+xml",
            headers=headers if download else None,
        )

    @router.post(
        "/auth/break-glass",
        tags=["auth"],
        summary="Log in the break-glass administrator",
        response_model=Me,
        responses=_ok(ex.ME, problems=False),
    )
    async def auth_break_glass(
        body: BreakGlassIn, request: Request, response: Response, svc: Services
    ) -> dict[str, Any]:
        """Log in the local administrator (password + TOTP). Off unless enabled."""
        sessions = require_sessions(svc)
        try:
            result = await sessions.break_glass_login(
                body.username, body.password, body.totp, client_source(request)
            )
        except LoginError as exc:
            raise ApiError(401, str(exc)) from exc
        set_session_cookie(request, response, result.cookie, result.session)
        return await me_payload(svc, result.session.principal(), result.session)

    @router.post("/auth/logout", status_code=204, tags=["auth"], summary="Log out")
    async def auth_logout(request: Request, who: FullWho, svc: Services) -> Response:
        """End the session (requires the CSRF header)."""
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie and svc.sessions is not None:
            await svc.sessions.logout(cookie)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    async def me_payload(
        svc: ApiServices, who: Principal, session: SessionInfo | None
    ) -> dict[str, Any]:
        tenants = await svc.tenants.list_tenants(who)
        via = await svc.sessions.explain(session) if session and svc.sessions else []
        return {
            "id": str(session.id) if session else None,
            "principal": who.actor,
            "name": session.name if session else who.actor,
            "email": session.email if session else None,
            "is_system": who.is_system,
            "break_glass": bool(session and session.break_glass),
            "auth": "session" if session else "token",
            "csrf_token": session.csrf_token if session else None,
            "session": {
                "expires_at": session.expires_at,
                "idle_timeout_seconds": int(session.idle_timeout.total_seconds()),
                "grants_computed_at": session.grants_computed_at or session.expires_at,
            }
            if session
            else None,
            "tenants": [
                {
                    "id": str(t.id),
                    "name": t.name,
                    "role": who.role_in(t.id).value,
                    "via": [
                        {"provider": g.provider, "group": g.group}
                        for g in via
                        if g.tenant_id in (None, t.id)
                    ],
                }
                for t in tenants
            ],
            "links": {"grafana": grafana_url},
            "features": {"lxc_guests": await svc.settings.lxc_allowed()},
        }

    @router.post(
        "/auth/refresh",
        tags=["auth"],
        summary="Recompute my roles now",
        response_model=Me,
        responses=_ok(ex.ME),
    )
    async def auth_refresh(request: Request, who: FullWho, svc: Services) -> dict[str, Any]:
        """Recompute grants from the stored provider groups and the current mappings.

        Role changes otherwise apply within a minute; this applies them at once. The session's
        absolute lifetime is not extended. A person left with no grant is signed out (401
        `session_expired`). Needs the CSRF header; API tokens just get their `Me` back.
        """
        cookie = request.cookies.get(SESSION_COOKIE)
        if request.headers.get("authorization") or not cookie or svc.sessions is None:
            return await me_payload(svc, who, None)
        try:
            info = await svc.sessions.refresh(cookie)
        except UserDisabledError:
            raise ApiError(
                403, "your account has been disabled by an administrator", code="user_disabled"
            ) from None
        if info is None:
            raise ApiError(401, "your session has expired; log in again", code="session_expired")
        return await me_payload(svc, info.principal(), info)

    @router.get(
        "/permissions",
        tags=["auth"],
        summary="The permission matrix",
        response_model=PermissionMatrix,
        responses=_ok(ex.PERMISSIONS),
    )
    async def get_permissions(who: FullWho) -> dict[str, Any]:
        """Which role may do what. `can(verb, object)` is `role_in(tenant) >= min_role`."""
        return {
            "roles": [r.value for r in permissions.ROLES],
            "permissions": [
                {
                    "verb": p.verb,
                    "object": p.object,
                    "min_role": p.min_role.value,
                    "description": p.description,
                    "group": p.group,
                }
                for p in permissions.MATRIX
            ],
        }

    @router.get(
        "/me",
        tags=["auth"],
        summary="Who am I",
        response_model=Me,
        responses=_ok(ex.ME),
    )
    async def me(request: Request, who: FullWho, svc: Services) -> dict[str, Any]:
        """Who am I, in which tenants, and with which role in each."""
        return await me_payload(svc, who, getattr(request.state, "session", None))

    # tenants
    @router.get(
        "/tenants",
        tags=["tenants"],
        summary="List tenants",
        response_model=list[TenantRead],
        responses=_ok([ex.TENANT_READ]),
    )
    async def list_tenants(who: Who, svc: Services) -> list[dict[str, Any]]:
        return [t.__dict__ | {"id": str(t.id)} for t in await svc.tenants.list_tenants(who)]

    @router.post(
        "/tenants",
        status_code=201,
        tags=["tenants"],
        summary="Create a tenant",
        response_model=TenantRead,
        responses=_ok(ex.TENANT_READ, 201) | VALIDATION_RESPONSE,
    )
    async def create_tenant(body: TenantIn, who: Who, svc: Services) -> dict[str, Any]:
        t = await svc.tenants.create(
            who,
            body.name,
            quotas=body.quotas,
            require_approval=body.require_approval,
            request_id=_rid(),
        )
        return t.__dict__ | {"id": str(t.id)}

    @router.get(
        "/tenants/{t}",
        tags=["tenants"],
        summary="Get a tenant",
        response_model=TenantRead,
        responses=_ok(ex.TENANT_READ, headers=ETAG_HEADER),
    )
    async def get_tenant(tenant: Tenant, response: Response) -> dict[str, Any]:
        response.headers["ETag"] = _etag(tenant.version)
        return tenant.__dict__ | {"id": str(tenant.id)}

    @router.patch(
        "/tenants/{t}",
        tags=["tenants"],
        summary="Change a tenant",
        response_model=TenantRead,
        responses=_ok(ex.TENANT_READ) | IF_MATCH_RESPONSES | VALIDATION_RESPONSE,
    )
    async def patch_tenant(
        body: TenantPatch,
        tenant: Tenant,
        who: Who,
        svc: Services,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        t = await svc.tenants.update(
            who,
            tenant.id,
            expected_version=_if_match(if_match),
            changes=body.model_dump(exclude_unset=True),
            request_id=_rid(),
        )
        return t.__dict__ | {"id": str(t.id)}

    @router.delete("/tenants/{t}", status_code=204, tags=["tenants"], summary="Delete a tenant")
    async def delete_tenant(tenant: Tenant, who: Who, svc: Services) -> Response:
        await svc.tenants.delete(who, tenant.id, request_id=_rid())
        return Response(status_code=204)

    for slug, object_type in COLLECTIONS.items():
        _object_routes(router, slug, object_type)
        if object_type in RENDERED_TYPES:
            _rendered_route(router, slug, object_type)

    @router.post(
        "/tenants/{t}/health-monitors/{monitor_id}/test",
        tags=["health-monitors"],
        summary="Test a health monitor against one pool member",
        description=(
            "Runs the monitor once from an engine (through its agent) against one member and "
            "returns pass or fail, the latency and what came back. Nothing is saved. Overrides "
            "(`port`, `http_path`, `expect_status`, `timeout_s`) replace the monitor's values for "
            "this run. The agent only probes members that are in its applied configuration, so "
            "a member added in a draft cannot be tested until it is applied (`409`). A UDP test "
            "needs the member to reply. `502` when the engine's agent cannot be reached."
        ),
        response_model=MonitorTestOut,
        responses=_ok(
            ex.MONITOR_TEST,
            extra={
                502: {"model": ProblemOut, "description": "The engine's agent cannot be reached."}
            },
        ),
    )
    async def test_monitor(
        monitor_id: UUID, body: MonitorTestIn, tenant: Tenant, who: Who, svc: Services
    ) -> MonitorTestOut:
        try:
            result = await svc.monitor_tests.test(
                who,
                tenant.id,
                monitor_id,
                pool_id=body.pool_id,
                member_id=body.member_id,
                engine_id=body.engine_id,
                port=body.port,
                http_path=body.http_path,
                expect_status=tuple(body.expect_status) if body.expect_status is not None else None,
                timeout_s=body.timeout_s,
            )
        except AgentError as exc:
            raise ApiError(502, str(exc)) from exc
        return MonitorTestOut(**result.__dict__)

    # telemetry
    async def _service_name(svc: ApiServices, tenant: TenantRecord, service_id: UUID) -> str:
        from switchyard.db.repository import load_snapshot

        async with tenant_session(svc.db.app, [tenant.id]) as session:
            snapshot = await load_snapshot(session)
        found = next((s for s in snapshot.virtual_services if s.id == service_id), None)
        if found is None:
            raise ApiError(404, f"virtual-services {service_id} not found")
        return found.name

    @router.get(
        "/tenants/{t}/virtual-services/{object_id}/metrics",
        tags=["virtual-services"],
        summary="Metrics of a virtual service",
        description=(
            "Requests per second, 95th percentile response time, 5xx ratio and current sessions "
            "over the last `window_s` seconds, one point per `step_s`, plus the latest value of "
            "each as `summary`. Read from Prometheus and Loki with the tenant label enforced. "
            "`available: false` (with a `reason`) when no backend is configured or it fails; a "
            "single value is `null` when the backend has no data for it, which is not zero. "
            "Requires any role in the tenant."
        ),
        response_model=MetricsRead,
        responses=_ok(ex.METRICS),
    )
    async def service_metrics(
        object_id: UUID,
        tenant: Tenant,
        who: Who,
        svc: Services,
        window_s: Annotated[
            int, Query(ge=60, le=604800, description="Seconds back from now.")
        ] = 3600,
        step_s: Annotated[
            int | None,
            Query(ge=5, le=3600, description="Seconds between points; default fits ~120 points."),
        ] = None,
    ) -> MetricsRead:
        name = await _service_name(svc, tenant, object_id)
        if svc.telemetry is None:
            return MetricsRead(available=False, reason="no metrics backend is configured")
        step = step_s or max(5, window_s // 120)
        try:
            found = await svc.telemetry.metrics(tenant.id, name, window_s=window_s, step_s=step)
        except TelemetryError as exc:
            return MetricsRead(available=False, reason=str(exc))
        return MetricsRead(available=True, metrics=found)

    @router.get(
        "/tenants/{t}/metrics/virtual-services",
        tags=["virtual-services"],
        summary="Headline metrics of every virtual service",
        description=(
            "Requests per second, 5xx ratio and current sessions per service over the last "
            "`window_s` seconds, for the list columns. `p95_ms` is only in the per-service "
            "route. `available: false` when no backend is configured. Requires any role in "
            "the tenant."
        ),
        response_model=MetricsOverview,
        responses=_ok(ex.METRICS_OVERVIEW),
    )
    async def metrics_overview(
        tenant: Tenant,
        who: Who,
        svc: Services,
        window_s: Annotated[
            int, Query(ge=60, le=86400, description="Seconds back from now.")
        ] = 900,
    ) -> MetricsOverview:
        from switchyard.db.repository import load_snapshot

        async with tenant_session(svc.db.app, [tenant.id]) as session:
            snapshot = await load_snapshot(session)
        if svc.telemetry is None:
            return MetricsOverview(
                available=False,
                reason="no metrics backend is configured",
                window_s=window_s,
                items=[],
            )
        names = sorted(s.name for s in snapshot.virtual_services if s.enabled)
        try:
            found = await svc.telemetry.summaries(tenant.id, names, window_s=window_s)
        except TelemetryError as exc:
            return MetricsOverview(available=False, reason=str(exc), window_s=window_s, items=[])
        by_name = {s.name: s.id for s in snapshot.virtual_services}
        return MetricsOverview(
            available=True,
            window_s=window_s,
            items=[
                MetricsOverviewItem(virtual_service_id=by_name[n], name=n, summary=v)
                for n, v in sorted(found.items())
            ],
        )

    @router.get(
        "/tenants/{t}/virtual-services/{object_id}/logs",
        tags=["virtual-services"],
        summary="Access log of a virtual service",
        description=(
            "HAProxy HTTP log lines of the service, newest first, parsed into fields. Filter "
            "with `since`/`until` (default: the last hour), `q` (substring of the raw line) and "
            "`status_class` (2, 3, 4 or 5). Page with `limit` and `cursor`. Read from Loki with "
            "the tenant label enforced; `available: false` when no log backend is configured. "
            "Requires any role in the tenant."
        ),
        response_model=AccessLogs,
        responses=_ok(ex.ACCESS_LOGS),
    )
    async def service_logs(
        object_id: UUID,
        tenant: Tenant,
        who: Who,
        svc: Services,
        since: datetime | None = None,
        until: datetime | None = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        status_class: Annotated[int | None, Query(ge=2, le=5)] = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> AccessLogs:
        name = await _service_name(svc, tenant, object_id)
        if svc.telemetry is None:
            return AccessLogs(available=False, reason="no log backend is configured", entries=[])
        end = until or datetime.now(UTC)
        if cursor:
            try:
                end = _EPOCH + timedelta(microseconds=int(cursor))
            except ValueError:
                raise ApiError(400, "cursor is not valid") from None
        start = since or end - timedelta(hours=1)
        try:
            found = await svc.telemetry.logs(
                tenant.id,
                name,
                since=start,
                until=end,
                limit=limit + 1,
                contains=q,
                status_class=status_class,
            )
        except TelemetryError as exc:
            return AccessLogs(available=False, reason=str(exc), entries=[])
        window = found[:limit]
        more = len(found) > limit
        return AccessLogs(
            available=True,
            entries=window,
            next_cursor=(
                str((window[-1].at - _EPOCH) // timedelta(microseconds=1)) if more else None
            ),
        )

    # search and summary
    @router.get(
        "/tenants/{t}/search",
        tags=["search"],
        summary="Find objects by name",
        description=(
            "Backs the command palette. Case-insensitive substring match on object names; "
            "virtual services also match their VIP, certificates their SANs and pools their "
            "member addresses. Best match first (exact, prefix, substring). Restrict with "
            "`types` (repeat the parameter). Requires any role in the tenant."
        ),
        response_model=SearchResults,
        responses=_ok(ex.SEARCH),
    )
    async def search_objects(
        tenant: Tenant,
        who: Who,
        svc: Services,
        q: Annotated[str, Query(min_length=1, max_length=200)],
        types: Annotated[list[ObjectType] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> SearchResults:
        from switchyard.db.repository import load_snapshot

        async with tenant_session(svc.db.app, [tenant.id]) as session:
            snapshot = await load_snapshot(session)
        return overview.search(snapshot, q, types or list(ObjectType), limit)

    @router.get(
        "/tenants/{t}/summary",
        tags=["status"],
        summary="Tenant home summary",
        description=(
            "One call for the tenant home: virtual services by health, pool members that are "
            "not `UP`, drifted and unreachable engines, certificates expiring within 30 days, "
            "job counts, open drafts and the latest revision. Lists are capped at 50. Health "
            "rules are those of `GET /status/virtual-services`. Requires any role in the tenant."
        ),
        response_model=TenantSummary,
        responses=_ok(ex.SUMMARY),
    )
    async def tenant_summary(tenant: Tenant, who: Who, svc: Services) -> TenantSummary:
        from sqlalchemy import func, select

        from switchyard.db.ops_models import ChangeSetRow, JobRow
        from switchyard.db.repository import load_snapshot

        statuses = await svc.status.list_status(who, tenant.id)
        async with tenant_session(svc.db.app, [tenant.id]) as session:
            snapshot = await load_snapshot(session)
            jobs = {
                r[0]: r[1]
                for r in await session.execute(
                    select(JobRow.state, func.count()).group_by(JobRow.state)
                )
            }
            drafts = {
                r[0]: r[1]
                for r in await session.execute(
                    select(ChangeSetRow.state, func.count()).group_by(ChangeSetRow.state)
                )
            }
        revisions = await svc.change_sets.revisions(who, tenant.id, limit=1)
        latest = (
            overview.LatestRevision(
                number=revisions[0].number,
                author=revisions[0].author,
                summary=revisions[0].summary,
                created_at=revisions[0].created_at,
            )
            if revisions
            else None
        )
        return overview.summarize(
            snapshot=snapshot,
            statuses=statuses,
            services=virtual_service_states(snapshot, statuses),
            jobs=overview.JobCounts(
                queued=jobs.get(JobState.QUEUED.value, 0),
                running=jobs.get(JobState.RUNNING.value, 0),
                failed=jobs.get(JobState.FAILED.value, 0) + jobs.get(JobState.DEAD.value, 0),
            ),
            open_drafts=drafts.get(ChangeSetState.DRAFT.value, 0),
            pending_approval=drafts.get(ChangeSetState.PENDING_APPROVAL.value, 0),
            latest_revision=latest,
            now=datetime.now(UTC),
        )

    @router.get(
        "/tenants/{t}/vip-pools/{pool_id}/allocations",
        tags=["vip-pools"],
        summary="Usage and allocations of a VIP pool",
        description=(
            "How many addresses the pool has, how many are held and by which virtual service. An "
            "address is held by the service that carries it as `vip`; there is no separate "
            "reservation, so *releasing* an address means changing the service's `vip` or "
            "deleting the service in a change set. With `change_set` the answer is the pool as "
            "that draft would leave it (`draft_status` per holder). `next_free` is the address a "
            "new service would get. Requires any role in the tenant."
        ),
        response_model=VipPoolUsage,
        responses=_ok(ex.VIP_POOL_USAGE),
    )
    async def vip_pool_allocations(
        pool_id: UUID,
        tenant: Tenant,
        who: Who,
        svc: Services,
        change_set: UUID | None = None,
    ) -> VipPoolUsage:
        return await _pool_usage(svc, who, tenant, pool_id, change_set)

    @router.post(
        "/tenants/{t}/vip-pools/{pool_id}/next-free",
        tags=["vip-pools"],
        summary="Suggest the next free address of a VIP pool",
        description=(
            "Returns the lowest free usable address. Nothing is reserved: two callers can get "
            "the same address and the validator rejects the second service that uses it. `409` "
            "when the pool is exhausted. Requires any role in the tenant."
        ),
        response_model=NextFree,
        responses=_ok(ex.NEXT_FREE),
    )
    async def vip_pool_next_free(
        pool_id: UUID,
        tenant: Tenant,
        who: Who,
        svc: Services,
        change_set: UUID | None = None,
    ) -> NextFree:
        usage = await _pool_usage(svc, who, tenant, pool_id, change_set)
        if usage.next_free is None:
            raise ApiError(409, "the VIP pool has no free address")
        return NextFree(address=usage.next_free)

    # change sets
    @router.get(
        "/tenants/{t}/change-sets",
        tags=["change-sets"],
        summary="List change sets",
        response_model=Page[ChangeSetSummary],
        responses=_ok({"items": [ex.CHANGE_SET_SUMMARY], "next_cursor": None}),
    )
    async def list_change_sets(
        tenant: Tenant,
        who: Who,
        svc: Services,
        state: ChangeSetState | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Page[Any]:
        """List change sets, newest first, with their age, base revision and operation count."""
        records = await svc.change_sets.list_change_sets(
            who,
            tenant.id,
            states={state} if state else None,
            limit=limit + 1,
            before=_uuid_cursor(cursor),
        )
        window = records[:limit]
        counts = await svc.change_sets.op_counts(who, tenant.id, [r.id for r in window])
        return Page(
            items=[
                ChangeSetSummary(
                    id=r.id,
                    state=r.state,
                    author=r.author,
                    summary=r.summary,
                    base_revision=r.base_revision,
                    created_at=r.created_at,
                    op_count=counts.get(r.id, 0),
                )
                for r in window
            ],
            next_cursor=str(window[-1].id) if len(records) > limit else None,
        )

    @router.post(
        "/tenants/{t}/change-sets",
        status_code=201,
        tags=["change-sets"],
        summary="Open a draft change set",
        response_model=ChangeSetRead,
        responses=_ok(ex.CHANGE_SET_READ, 201),
    )
    async def open_change_set(
        body: OpenIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        return _detail_json(
            await svc.change_sets.open(who, tenant.id, body.summary, request_id=_rid())
        )

    @router.get(
        "/tenants/{t}/change-sets/{cs}",
        tags=["change-sets"],
        summary="Get a change set with its operations",
        response_model=ChangeSetRead,
        responses=_ok(ex.CHANGE_SET_READ),
    )
    async def get_change_set(cs: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, Any]:
        return _detail_json(await svc.change_sets.get(who, tenant.id, cs))

    @router.post(
        "/tenants/{t}/change-sets/{cs}/ops",
        tags=["change-sets"],
        summary="Add operations to a draft",
        response_model=ChangeSetRead,
        responses=_ok(ex.CHANGE_SET_READ) | VALIDATION_RESPONSE,
    )
    async def add_ops(
        cs: UUID, body: OpsIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        ops = [ChangeOp(**o.model_dump()) for o in body.ops]
        return _detail_json(
            await svc.change_sets.add_ops(who, tenant.id, cs, ops, request_id=_rid())
        )

    @router.delete(
        "/tenants/{t}/change-sets/{cs}/ops/{index}",
        tags=["change-sets"],
        summary="Remove an operation from a draft",
        response_model=ChangeSetRead,
        responses=_ok(ex.CHANGE_SET_READ),
    )
    async def remove_op(
        cs: UUID, index: int, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        return _detail_json(
            await svc.change_sets.remove_op(who, tenant.id, cs, index, request_id=_rid())
        )

    @router.get(
        "/tenants/{t}/change-sets/{cs}/diff",
        tags=["change-sets"],
        summary="Preview what a change set would change",
        response_model=ChangeSetDiff,
        responses=_ok(ex.DIFF),
    )
    async def diff(cs: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, Any]:
        """Objects touched, rule `problems` (block apply), `warnings` (never block) and files."""
        preview = await svc.change_sets.preview(who, tenant.id, cs)
        base, _ = await svc.change_sets.draft_view(who, tenant.id, cs)
        found = impact_of(base, preview.draft)
        names = {g.id: g.name for snap in (base, preview.draft.snapshot) for g in snap.se_groups}
        return {
            "impact": {
                "services": [
                    {"id": str(i), "name": n, "vip": v, "se_group_id": str(g)}
                    for i, n, v, g in found.services
                ],
                "vips": list(found.vips),
                "se_groups": [{"id": str(g), "name": names[g]} for g in found.se_group_ids],
                "hostnames": list(found.hostnames),
            },
            "objects": [
                {
                    "type": o.object_type.value,
                    "id": str(o.object_id),
                    "name": o.name,
                    "status": o.status.value,
                    "changed_fields": list(o.changed_fields),
                }
                for o in preview.draft.objects
            ],
            "problems": [p.__dict__ for p in preview.draft.problems],
            "warnings": [w.__dict__ for w in preview.warnings],
            "se_groups": [
                {
                    "id": str(g.se_group_id),
                    "name": g.name,
                    "error": g.error,
                    "files": [f.__dict__ for f in g.files],
                }
                for g in preview.groups
            ],
        }

    @router.post(
        "/tenants/{t}/change-sets/{cs}/apply",
        status_code=202,
        tags=["change-sets"],
        summary="Apply a change set",
        response_model=JobAccepted,
        responses=_ok(ex.JOB_ACCEPTED, 202) | VALIDATION_RESPONSE,
    )
    async def apply(
        cs: UUID, tenant: Tenant, who: Who, svc: Services, body: ApplyIn | None = None
    ) -> dict[str, str]:
        job = await svc.change_sets.apply(
            who,
            tenant.id,
            cs,
            break_glass_reason=body.break_glass_reason if body else None,
            message=body.message if body else None,
            request_id=_rid(),
        )
        return {"job_id": str(job)}

    @router.get(
        "/tenants/{t}/virtual-services/{service}/engines",
        tags=["virtual-services"],
        summary="Engines a virtual service is placed on",
        description=(
            "The enabled engines of the service's SE group, by name: where its configuration is "
            "pushed. Requires any role in the tenant."
        ),
        response_model=list[ServiceEngineRead],
        responses=_ok(ex.SERVICE_ENGINES),
    )
    async def service_engines(
        service: UUID, tenant: Tenant, who: Who, svc: Services
    ) -> list[dict[str, Any]]:
        records = await svc.status.engines_of_service(who, tenant.id, service)
        return [
            {"id": r.id, "name": r.name, "mgmt_ip": r.mgmt_ip, "state": r.state.value}
            for r in records
        ]

    @router.get(
        "/tenants/{t}/virtual-services/{service}/engines/{engine}/rendered",
        tags=["virtual-services"],
        summary="Rendered configuration for one engine of a service",
        description=(
            "All files the controller renders for the engine: the HAProxy files (`scope: group`, "
            "the same on every engine of the SE group) and the engine's own routing files "
            "(`scope: engine`, FRR or Keepalived). Certificates and keys are never included and "
            "BGP passwords read `REDACTED`. `404` if the engine does not serve the service. "
            "Requires any role in the tenant."
        ),
        response_model=EngineRenderedRead,
        responses=_ok(ex.ENGINE_RENDERED),
    )
    async def service_engine_rendered(
        service: UUID, engine: UUID, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        try:
            record, fingerprint, files = await svc.status.rendered_for_engine(
                who, tenant.id, service, engine
            )
        except RenderError as exc:
            raise ApiError(422, f"cannot render: {exc}") from exc
        return {
            "engine": record.name,
            "engine_id": record.id,
            "se_group_id": record.se_group_id,
            "fingerprint": fingerprint,
            "files": [
                {
                    "file": path,
                    "scope": "group"
                    if path.startswith("haproxy/") or path.startswith("certs/")
                    else "engine",
                    "language": "haproxy" if path.endswith(".cfg") else "plaintext",
                    "content": content,
                }
                for path, content in sorted(files.items())
            ],
        }

    @router.get(
        "/tenants/{t}/engines/{engine}/running-config",
        tags=["engines"],
        summary="What an engine is running now, compared with the desired state",
        description=(
            "Asks the engine live: HAProxy's running configuration from its Data Plane API with "
            "the fingerprint it carries and whether it equals what the desired state renders, "
            "and, through the agent, the hash of every routing file against the hash of what the "
            "controller would send now (the agent cannot read file contents back, by design). "
            "Not stored. For tenants an unreachable engine reports a fixed `error` sentence. "
            "Requires any role in the tenant."
        ),
        response_model=RunningConfigRead,
        responses=_ok(ex.RUNNING_CONFIG),
    )
    async def engine_running_config(
        engine: UUID, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        out = await svc.status.running_config(who, tenant.id, engine)
        if out["error"] and not who.is_system:
            out["error"] = GENERIC_ENGINE_ERROR
        return out

    @router.post(
        "/tenants/{t}/se-groups/{group}/reapply",
        status_code=202,
        tags=["se-groups"],
        summary="Re-apply the desired state to an SE group (drift repair)",
        description=(
            "Pushes the tenant's current desired state to every engine of the group again, canary "
            "first, to put engines that drifted back to what the database says. It makes no "
            "revision and changes no desired state. Drift is never corrected on its own; this is "
            "the explicit action. Returns the job (`reapply_se_group`); its result has `ok`, "
            "`revision_number` (the revision that was pushed) and `error`. Requires "
            "`tenant-admin`, because it overwrites what the engines run now. `404` if the group "
            "does not exist."
        ),
        response_model=JobAccepted,
        responses=_ok(ex.JOB_ACCEPTED, 202),
    )
    async def reapply_group(group: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, str]:
        job = await svc.change_sets.reapply_group(who, tenant.id, group, request_id=_rid())
        return {"job_id": str(job)}

    @router.post(
        "/tenants/{t}/change-sets/{cs}/discard",
        tags=["change-sets"],
        summary="Discard a draft",
        response_model=StateOut,
        responses=_ok(ex.STATE_OUT),
    )
    async def discard(cs: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, str]:
        r = await svc.change_sets.discard(who, tenant.id, cs, request_id=_rid())
        return {"state": r.state.value}

    @router.post(
        "/tenants/{t}/change-sets/{cs}/request-approval",
        tags=["change-sets"],
        summary="Ask for approval",
        response_model=StateOut,
        responses=_ok({"state": "pending_approval"}),
    )
    async def request_approval(cs: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, str]:
        r = await svc.change_sets.request_approval(who, tenant.id, cs, request_id=_rid())
        return {"state": r.state.value}

    @router.post(
        "/tenants/{t}/change-sets/{cs}/approve",
        status_code=202,
        tags=["change-sets"],
        summary="Approve and apply a change set",
        response_model=JobAccepted,
        responses=_ok(ex.JOB_ACCEPTED, 202),
    )
    async def approve(cs: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, str]:
        return {"job_id": str(await svc.change_sets.approve(who, tenant.id, cs, request_id=_rid()))}

    @router.post(
        "/tenants/{t}/change-sets/{cs}/reject",
        tags=["change-sets"],
        summary="Reject a change set",
        response_model=StateOut,
        responses=_ok({"state": "draft"}),
    )
    async def reject(
        cs: UUID, body: ReasonIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, str]:
        r = await svc.change_sets.reject(who, tenant.id, cs, body.comment, request_id=_rid())
        return {"state": r.state.value}

    # revisions
    @router.get(
        "/tenants/{t}/revisions",
        tags=["revisions"],
        summary="List revisions",
        description=(
            "Applied revisions, newest first. `object` (an object id of any type) keeps the "
            "revisions whose change set had an operation on that object: the History tab of an "
            "object page. Page with `limit` and `cursor`. Requires any role in the tenant."
        ),
        response_model=Page[RevisionSummary],
        responses=_ok({"items": [ex.REVISION_SUMMARY], "next_cursor": None}),
    )
    async def revisions(
        tenant: Tenant,
        who: Who,
        svc: Services,
        object: UUID | None = None,  # noqa: A002 - the query parameter is part of the API
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Page[Any]:
        """List applied revisions, newest first; ``object`` keeps those that touched an object."""
        records = await svc.change_sets.revisions(
            who, tenant.id, limit=limit + 1, before=_int_cursor(cursor), object_id=object
        )
        window = records[:limit]
        return Page(
            items=[
                RevisionSummary(
                    number=r.number,
                    author=r.author,
                    summary=r.summary,
                    message=r.message,
                    git_sha=r.git_sha,
                    created_at=r.created_at,
                )
                for r in window
            ],
            next_cursor=str(window[-1].number) if len(records) > limit else None,
        )

    @router.get(
        "/tenants/{t}/revisions/{n}",
        tags=["revisions"],
        summary="Get a revision with its full desired state",
        response_model=RevisionRead,
        responses=_ok(ex.REVISION_READ),
    )
    async def revision(n: int, tenant: Tenant, who: Who, svc: Services) -> dict[str, Any]:
        r = await svc.change_sets.revision(who, tenant.id, n)
        return {
            "number": r.number,
            "author": r.author,
            "summary": r.summary,
            "message": r.message,
            "git_sha": r.git_sha,
            "created_at": r.created_at,
            "snapshot": r.snapshot,
        }

    @router.post(
        "/tenants/{t}/revisions/{n}/rollback",
        status_code=201,
        tags=["revisions"],
        summary="Open a change set that restores a revision",
        response_model=ChangeSetRead,
        responses=_ok(ex.CHANGE_SET_READ, 201),
    )
    async def rollback(n: int, tenant: Tenant, who: Who, svc: Services) -> dict[str, Any]:
        return _detail_json(await svc.change_sets.rollback(who, tenant.id, n, request_id=_rid()))

    # runtime
    @router.put(
        "/tenants/{t}/pools/{pool}/members/{member}/state",
        tags=["pools"],
        summary="Set a pool member's runtime state",
        response_model=MemberStateResult,
        responses=_ok(ex.MEMBER_STATE),
    )
    async def member_state(
        pool: UUID, member: UUID, body: MemberStateIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        r = await svc.members.set_state(who, tenant.id, pool, member, body.state, request_id=_rid())
        return {"state": r.state.value, "applied": r.applied, "failed": r.failed}

    # engines
    @router.get(
        "/tenants/{t}/se-groups/{group}/engines",
        tags=["engines"],
        summary="List an SE group's engines",
        response_model=list[EngineRead],
        responses=_ok([ex.ENGINE_READ]),
    )
    async def list_engines(
        group: UUID, tenant: Tenant, who: Who, svc: Services
    ) -> list[dict[str, Any]]:
        online = await svc.hosts.online_by_node() if svc.hosts else {}
        return [
            _engine_json(d, online)
            for d in await svc.engines.describe(who, tenant.id, se_group_id=group)
        ]

    @router.get(
        "/tenants/{t}/se-groups/{group}/placement",
        tags=["engines"],
        summary="Where an SE group's engines run on Proxmox",
        description=(
            "The Proxmox cluster nodes with online state, load, memory and storage, and the "
            "group's engines on each: the data of the Placement card. Read from Proxmox and "
            "cached for 15 seconds. When Proxmox is not configured or not reachable the answer is "
            "`available: false` with the nodes the engines claim to run on and `online: null`. "
            "Requires any role in the tenant."
        ),
        response_model=PlacementRead,
        responses=_ok(ex.PLACEMENT),
    )
    async def se_group_placement(
        group: UUID, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        if svc.hosts is None:
            raise ApiError(503, "host information is not available in this deployment")
        p = await svc.hosts.placement(who, tenant.id, group)
        return {
            "available": p.available,
            "reason": p.reason,
            "nodes": [
                n.__dict__
                | {
                    "storage": [s.__dict__ | {"content": list(s.content)} for s in n.storage],
                    "engines": [e.__dict__ for e in n.engines],
                }
                for n in p.nodes
            ],
        }

    @router.get(
        "/tenants/{t}/engines",
        tags=["engines"],
        summary="List every engine of the tenant",
        description=(
            "All service engines across SE groups, by group then name, with Proxmox placement "
            "(`pve_node`, `vmid`) and agent enrollment. Filter with `se_group` and `state`. "
            "Live health is in `GET /status/engines`. Requires any role in the tenant."
        ),
        response_model=list[EngineRead],
        responses=_ok([ex.ENGINE_READ]),
    )
    async def list_tenant_engines(
        tenant: Tenant,
        who: Who,
        svc: Services,
        se_group: UUID | None = None,
        state: EngineState | None = None,
    ) -> list[dict[str, Any]]:
        online = await svc.hosts.online_by_node() if svc.hosts else {}
        return [
            _engine_json(d, online)
            for d in await svc.engines.describe(who, tenant.id, se_group_id=se_group, state=state)
        ]

    @router.get(
        "/tenants/{t}/engines/{engine}",
        tags=["engines"],
        summary="Get one engine",
        description="One engine with placement and enrollment. `404` if it is not in the tenant.",
        response_model=EngineRead,
        responses=_ok(ex.ENGINE_READ),
    )
    async def get_engine(engine: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, Any]:
        found = await svc.engines.describe(who, tenant.id, engine_id=engine)
        if not found:
            raise ApiError(404, f"engine {engine} not found")
        online = await svc.hosts.online_by_node() if svc.hosts else {}
        return _engine_json(found[0], online)

    @router.post(
        "/tenants/{t}/se-groups/{group}/engines",
        status_code=201,
        tags=["engines"],
        summary="Register an engine",
        response_model=EngineRead,
        responses=_ok(ex.ENGINE_READ, 201) | VALIDATION_RESPONSE,
    )
    async def add_engine(
        group: UUID, body: EngineIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        e = await svc.engines.register(
            who,
            tenant.id,
            se_group_id=group,
            name=body.name,
            dataplane_url=body.dataplane_url,
            credentials_ref=body.credentials_ref,
            mgmt_ip=body.mgmt_ip,
            agent_url=body.agent_url,
            request_id=_rid(),
        )
        return _engine_json(EngineDetail(e), {})

    @router.put(
        "/tenants/{t}/engines/{engine}/state",
        tags=["engines"],
        summary="Enable or disable an engine",
        response_model=EngineStateOut,
        responses=_ok(ex.ENGINE_STATE_OUT),
    )
    async def engine_state(
        engine: UUID, state: EngineState, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, str]:
        e = await svc.engines.set_state(who, tenant.id, engine, state, request_id=_rid())
        return {"state": e.state.value}

    @router.delete(
        "/tenants/{t}/engines/{engine}",
        status_code=204,
        tags=["engines"],
        summary="Remove an engine record",
    )
    async def remove_engine(engine: UUID, tenant: Tenant, who: Who, svc: Services) -> Response:
        await svc.engines.remove(who, tenant.id, engine, request_id=_rid())
        return Response(status_code=204)

    # tokens
    @router.get(
        "/tenants/{t}/tokens",
        tags=["tokens"],
        summary="List API tokens",
        response_model=list[TokenRead],
        responses=_ok([ex.TOKEN_READ]),
    )
    async def list_tokens(tenant: Tenant, who: Who, svc: Services) -> list[dict[str, Any]]:
        return [
            {
                "id": str(r.id),
                "name": r.name,
                "role": r.role.value,
                "revoked": r.revoked_at is not None,
                "expires_at": r.expires_at,
            }
            for r in await svc.tokens.list_tokens(who, tenant.id)
        ]

    @router.post(
        "/tenants/{t}/tokens",
        status_code=201,
        tags=["tokens"],
        summary="Create an API token (shown once)",
        response_model=TokenCreated,
        responses=_ok(ex.TOKEN_CREATED, 201) | VALIDATION_RESPONSE,
    )
    async def create_token(
        body: TokenIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        from datetime import timedelta

        plaintext, record = await svc.tokens.create(
            who,
            tenant.id,
            body.name,
            body.role,
            ttl=timedelta(days=body.ttl_days) if body.ttl_days else None,
            request_id=_rid(),
        )
        return {"id": str(record.id), "token": plaintext, "role": record.role.value}

    @router.delete(
        "/tenants/{t}/tokens/{token}", status_code=204, tags=["tokens"], summary="Revoke a token"
    )
    async def revoke_token(token: UUID, tenant: Tenant, who: Who, svc: Services) -> Response:
        await svc.tokens.revoke(who, tenant.id, token, request_id=_rid())
        return Response(status_code=204)

    # identity providers
    def idp_json(request: Request, r: IdentityProviderRecord) -> dict[str, Any]:
        base = (request.app.state.public_url or str(request.base_url)).rstrip("/")
        out: dict[str, Any] = {
            "protocol": r.protocol,
            "id": str(r.id),
            "name": r.name,
            "enabled": r.enabled,
            "version": r.version,
            "created_at": r.created_at,
            "warnings": list(r.warnings),
        } | r.settings
        if r.protocol == "saml":
            out["sp_entity_id"] = (
                r.settings.get("sp_entity_id") or f"{base}/api/v1/auth/saml/{r.id}"
            )
            out["acs_url"] = f"{base}/api/v1/auth/callback/{r.id}"
            out["sp_metadata_url"] = f"{base}/api/v1/auth/saml/{r.id}/metadata"
        else:
            out.pop("client_secret_set", None)
            out["client_secret_set"] = bool(r.settings.get("client_secret_ref"))
        return out

    def require_idp(svc: ApiServices) -> IdentityProviderService:
        if svc.identity_providers is None:
            raise ApiError(503, "login is not configured on this controller")
        return svc.identity_providers

    @router.get(
        "/identity-providers",
        tags=["identity providers"],
        summary="List identity providers",
        response_model=list[IdentityProviderRead],
        responses=_ok([ex.IDP_OIDC, ex.IDP_SAML]),
    )
    async def list_identity_providers(
        request: Request, who: Who, svc: Services
    ) -> list[dict[str, Any]]:
        """OIDC and SAML providers, by name. Secrets are never returned."""
        return [idp_json(request, r) for r in await require_idp(svc).list_providers(who)]

    @router.post(
        "/identity-providers",
        status_code=201,
        tags=["identity providers"],
        summary="Create an identity provider",
        response_model=IdentityProviderRead,
        responses=_ok(ex.IDP_OIDC, 201) | VALIDATION_RESPONSE,
    )
    async def create_identity_provider(
        body: IdentityProviderIn, request: Request, who: Who, svc: Services
    ) -> dict[str, Any]:
        """Create a provider. `protocol` selects the schema.

        An OIDC `client_secret` is stored in the secrets backend and never returned. An OIDC
        provider whose discovery document cannot be fetched is still created; `warnings` says so.
        A SAML provider can be filled in from its metadata (`idp_metadata_url` or
        `idp_metadata_xml`).
        """
        data = body.model_dump(exclude_unset=True)
        record = await require_idp(svc).create(
            who,
            protocol=data.pop("protocol"),
            name=data.pop("name"),
            enabled=data.pop("enabled", True),
            settings=data,
            request_id=_rid(),
        )
        return idp_json(request, record)

    @router.get(
        "/identity-providers/{idp}",
        tags=["identity providers"],
        summary="Get an identity provider",
        response_model=IdentityProviderRead,
        responses=_ok(ex.IDP_OIDC, headers=ETAG_HEADER),
    )
    async def get_identity_provider(
        idp: UUID, request: Request, response: Response, who: Who, svc: Services
    ) -> dict[str, Any]:
        record = await require_idp(svc).get(who, idp)
        response.headers["ETag"] = _etag(record.version)
        return idp_json(request, record)

    @router.patch(
        "/identity-providers/{idp}",
        tags=["identity providers"],
        summary="Change an identity provider",
        response_model=IdentityProviderRead,
        responses=_ok(ex.IDP_OIDC) | IF_MATCH_RESPONSES | VALIDATION_RESPONSE,
    )
    async def patch_identity_provider(
        idp: UUID,
        body: IdentityProviderPatch,
        request: Request,
        response: Response,
        who: Who,
        svc: Services,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Change a provider. Send `protocol` (it cannot change) and only the fields to change."""
        data = body.model_dump(exclude_unset=True)
        current = await require_idp(svc).get(who, idp)
        if data.pop("protocol") != current.protocol:
            raise ApiError(
                422,
                "the protocol of a provider cannot change",
                errors=[{"path": "protocol", "code": "immutable", "message": "cannot change"}],
            )
        record = await require_idp(svc).update(
            who, idp, expected_version=_if_match(if_match), changes=data, request_id=_rid()
        )
        response.headers["ETag"] = _etag(record.version)
        return idp_json(request, record)

    @router.delete(
        "/identity-providers/{idp}",
        status_code=204,
        tags=["identity providers"],
        summary="Delete an identity provider",
        responses=IF_MATCH_RESPONSES,
    )
    async def delete_identity_provider(
        idp: UUID,
        who: Who,
        svc: Services,
        if_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """Delete a provider with its role mappings, its users and their sessions."""
        await require_idp(svc).delete(
            who, idp, expected_version=_if_match(if_match), request_id=_rid()
        )
        return Response(status_code=204)

    @router.post(
        "/identity-providers/{idp}/test",
        status_code=201,
        tags=["identity providers"],
        summary="Start a test sign-in",
        response_model=IdpTestStarted,
        responses=_ok(ex.IDP_TEST_STARTED, 201),
    )
    async def start_idp_test(
        idp: UUID,
        request: Request,
        who: Who,
        svc: Services,
        body: IdpTestIn | None = None,
    ) -> dict[str, Any]:
        """Open `authorize_url` in the browser; poll the result with `test_id`.

        The callback creates no session and no user. A disabled provider can be tested.
        """
        providers = require_idp(svc)
        sessions = require_sessions(svc)
        test_id = await providers.new_test(who, idp)
        try:
            url = await sessions.begin_login(
                str(idp),
                redirect_uri=callback_url(request, str(idp)),
                return_to=body.return_to if body else None,
                test_id=test_id,
            )
        except LoginError as exc:
            raise ApiError(422, str(exc)) from exc
        return {"test_id": str(test_id), "authorize_url": url}

    @router.get(
        "/identity-providers/{idp}/test/{test_id}",
        tags=["identity providers"],
        summary="Read the result of a test sign-in",
        response_model=IdpTestResult,
        responses=_ok(ex.IDP_TEST_RESULT),
    )
    async def read_idp_test(idp: UUID, test_id: UUID, who: Who, svc: Services) -> dict[str, Any]:
        """`pending` until the provider answered; then the result, once. Later reads are 404."""
        result = await require_idp(svc).read_test(who, idp, test_id)
        return {"status": result.status} | (result.result or {})

    # break-glass administration
    def break_glass_json(status: BreakGlassStatus) -> dict[str, Any]:
        admin = status.admin
        return {
            "forced_by_deployment": status.forced_by_deployment,
            "enabled": status.enabled,
            "enabled_until": status.enabled_until,
            "enabled_by": status.enabled_by,
            "reason": status.reason,
            "admin": None
            if admin is None
            else {
                "username": admin.username,
                "totp_confirmed": admin.totp_confirmed,
                "created_at": admin.created_at,
                "last_login_at": admin.last_login_at,
                "locked_until": admin.locked_until,
            },
        }

    def require_break_glass(svc: ApiServices) -> BreakGlassService:
        if svc.break_glass is None:
            raise ApiError(503, "login is not configured on this controller")
        return svc.break_glass

    @router.get(
        "/system/settings",
        tags=["system"],
        summary="Controller-wide settings",
        description=(
            "The settings in effect: today `allow_lxc_guests` (default off) and whether the "
            "deployment forces it on (`SWITCHYARD_ALLOW_LXC=1`). Requires `system-admin`; every "
            "signed-in user can read what they need from `GET /me` `features`."
        ),
        response_model=SystemSettingsRead,
        responses=_ok(ex.SYSTEM_SETTINGS),
    )
    async def get_system_settings(who: Who, svc: Services) -> dict[str, Any]:
        require(who, "manage", "tenant")
        return (await svc.settings.view()).__dict__

    @router.patch(
        "/system/settings",
        tags=["system"],
        summary="Change controller-wide settings",
        description=(
            "Send only the settings to change. `allow_lxc_guests` lets tenant administrators "
            "deploy SE groups as containers; while it is off a deploy with `guest_kind: lxc` "
            "answers `403` with code `lxc_disabled`. Audited as `system.settings_changed`. "
            "Requires `system-admin`."
        ),
        response_model=SystemSettingsRead,
        responses=_ok(ex.SYSTEM_SETTINGS) | VALIDATION_RESPONSE,
    )
    async def patch_system_settings(
        body: SystemSettingsIn, who: Who, svc: Services
    ) -> dict[str, Any]:
        view = await svc.settings.update(
            who, allow_lxc_guests=body.allow_lxc_guests, request_id=_rid()
        )
        return view.__dict__

    def require_acme(svc: ApiServices) -> AcmeProviders:
        if svc.acme_providers is None:
            raise ApiError(503, "ACME providers need a writable secrets backend (OpenBao)")
        return svc.acme_providers

    def require_dns(svc: ApiServices) -> DnsProviders:
        if svc.dns_providers is None:
            raise ApiError(503, "DNS credentials need a writable secrets backend (OpenBao)")
        return svc.dns_providers

    @router.get(
        "/acme-provider-presets",
        tags=["acme providers"],
        summary="Known certificate authorities",
        response_model=list[AcmePresetRead],
        responses=_ok(ex.ACME_PRESETS),
    )
    async def list_acme_presets(who: Who, svc: Services) -> list[dict[str, Any]]:
        """Let's Encrypt, ZeroSSL, Google, Buypass, Keyfactor and a custom entry."""
        return [asdict(p) for p in AcmeProviders.presets()]

    @router.get(
        "/acme-providers",
        tags=["acme providers"],
        summary="List ACME providers",
        response_model=list[AcmeProviderRead],
        responses=_ok([ex.ACME_PROVIDER]),
    )
    async def list_acme_providers(who: Who, svc: Services) -> list[dict[str, Any]]:
        """Every provider, by name. Everyone signed in can read them so they can choose one."""
        return [asdict(p) for p in await require_acme(svc).list_providers()]

    @router.post(
        "/acme-providers",
        status_code=201,
        tags=["acme providers"],
        summary="Register an ACME provider",
        response_model=AcmeProviderRead,
        responses=_ok(ex.ACME_PROVIDER, 201) | VALIDATION_RESPONSE,
    )
    async def create_acme_provider(body: AcmeProviderIn, who: Who, svc: Services) -> dict[str, Any]:
        """Register a certificate authority. The controller creates the ACME account key itself.

        Secrets (`eab_hmac_key`, `oauth_client_secret`) go to the secrets backend and are never
        returned. Call the test endpoint to register the account at the CA.
        """
        view = await require_acme(svc).create(
            who,
            name=body.name,
            preset=body.preset,
            directory_url=body.directory_url,
            email=body.email,
            auth_mode=body.auth_mode,
            eab_kid=body.eab_kid,
            eab_hmac_key=body.eab_hmac_key,
            oauth=body.oauth.model_dump(exclude_none=True) if body.oauth else None,
            oauth_client_secret=body.oauth_client_secret,
            account_key=body.account_key,
            ca_bundle_pem=body.ca_bundle_pem,
            terms_agreed=body.terms_agreed,
            enabled=body.enabled,
            default=body.default,
            request_id=_rid(),
        )
        return asdict(view)

    @router.get(
        "/acme-providers/{provider}",
        tags=["acme providers"],
        summary="Get an ACME provider",
        response_model=AcmeProviderRead,
        responses=_ok(ex.ACME_PROVIDER, headers=ETAG_HEADER),
    )
    async def get_acme_provider(
        provider: UUID, response: Response, who: Who, svc: Services
    ) -> dict[str, Any]:
        """One provider with its version in the `ETag`."""
        view = await require_acme(svc).get(provider)
        response.headers["ETag"] = _etag(view.version)
        return asdict(view)

    @router.patch(
        "/acme-providers/{provider}",
        tags=["acme providers"],
        summary="Change an ACME provider",
        response_model=AcmeProviderRead,
        responses=_ok(ex.ACME_PROVIDER) | IF_MATCH_RESPONSES | VALIDATION_RESPONSE,
    )
    async def patch_acme_provider(
        provider: UUID,
        body: AcmeProviderPatch,
        response: Response,
        who: Who,
        svc: Services,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Change settings or replace secrets.

        Changing the directory or the authentication means the account has to be registered again
        (run the test).
        """
        data = body.model_dump(exclude_unset=True, exclude_none=False)
        oauth = data.pop("oauth", None)
        if oauth is not None:
            oauth = {k: v for k, v in oauth.items() if v is not None}
        view = await require_acme(svc).update(
            who,
            provider,
            expected_version=_if_match(if_match),
            oauth=oauth,
            request_id=_rid(),
            **data,
        )
        response.headers["ETag"] = _etag(view.version)
        return asdict(view)

    @router.delete(
        "/acme-providers/{provider}",
        status_code=204,
        tags=["acme providers"],
        summary="Remove an ACME provider",
        responses=IF_MATCH_RESPONSES,
    )
    async def delete_acme_provider(
        provider: UUID,
        who: Who,
        svc: Services,
        if_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """Remove a provider that no certificate uses (`409` otherwise)."""
        await require_acme(svc).delete(
            who, provider, expected_version=_if_match(if_match), request_id=_rid()
        )
        return Response(status_code=204)

    @router.post(
        "/acme-providers/{provider}/test",
        tags=["acme providers"],
        summary="Register the account at the CA",
        response_model=AcmeProviderTestRead,
        responses=_ok(ex.ACME_PROVIDER_TEST),
    )
    async def test_acme_provider(provider: UUID, who: Who, svc: Services) -> dict[str, Any]:
        """Contact the CA, sign in (EAB or OAuth) and register the account or find it.

        Always answers `200`; `ok: false` carries the reason. A success sets `account_registered`.
        """
        return asdict(await require_acme(svc).test(who, provider, request_id=_rid()))

    @router.get(
        "/dns-provider-types",
        tags=["dns providers"],
        summary="DNS provider types and their forms",
        response_model=list[DnsProviderTypeRead],
        responses=_ok(ex.DNS_PROVIDER_TYPES),
    )
    async def list_dns_provider_types(who: Who, svc: Services) -> list[dict[str, Any]]:
        """Each type lists its fields; `secret` fields are write-only. Render the form from this."""
        return [asdict(t) for t in DnsProviders.catalogue()]

    @router.get(
        "/tenants/{t}/dns-providers",
        tags=["dns providers"],
        summary="List the tenant's DNS credentials",
        response_model=list[DnsProviderRead],
        responses=_ok([ex.DNS_PROVIDER]),
    )
    async def list_dns_providers(tenant: Tenant, who: Who, svc: Services) -> list[dict[str, Any]]:
        """The credentials DNS-01 can use, by name. Secrets are never returned."""
        return [asdict(p) for p in await require_dns(svc).list_providers(who, tenant.id)]

    @router.post(
        "/tenants/{t}/dns-providers",
        status_code=201,
        tags=["dns providers"],
        summary="Add a DNS credential",
        response_model=DnsProviderRead,
        responses=_ok(ex.DNS_PROVIDER, 201) | VALIDATION_RESPONSE,
    )
    async def create_dns_provider(
        body: DnsProviderIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        """Add a credential for a provider type.

        Validation errors name the field, for example `credentials.api_token`.
        """
        view = await require_dns(svc).create(
            who,
            tenant.id,
            name=body.name,
            type_=body.type,
            zones=body.zones,
            config=body.config,
            credentials=body.credentials,
            enabled=body.enabled,
            request_id=_rid(),
        )
        return asdict(view)

    @router.get(
        "/tenants/{t}/dns-providers/match",
        tags=["dns providers"],
        summary="Which credentials can validate these names",
        response_model=list[DnsNameMatchRead],
        responses=_ok(ex.DNS_MATCH),
    )
    async def match_dns_providers(
        tenant: Tenant,
        who: Who,
        svc: Services,
        names: Annotated[
            list[str], Query(description="Names to look up (repeat the parameter); wildcards ok.")
        ],
    ) -> list[dict[str, Any]]:
        """For each name, the tenant's enabled credentials whose zones cover it, longest zone first.

        A credential without a zone list comes last. An empty `candidates` list means the name
        cannot be validated with DNS-01 until a credential covers it.
        """
        return [asdict(m) for m in await require_dns(svc).match(who, tenant.id, names)]

    @router.get(
        "/tenants/{t}/dns-providers/{provider}",
        tags=["dns providers"],
        summary="Get a DNS credential",
        response_model=DnsProviderRead,
        responses=_ok(ex.DNS_PROVIDER, headers=ETAG_HEADER),
    )
    async def get_dns_provider(
        provider: UUID, tenant: Tenant, response: Response, who: Who, svc: Services
    ) -> dict[str, Any]:
        """One credential with its version in the `ETag`."""
        view = await require_dns(svc).get(who, tenant.id, provider)
        response.headers["ETag"] = _etag(view.version)
        return asdict(view)

    @router.patch(
        "/tenants/{t}/dns-providers/{provider}",
        tags=["dns providers"],
        summary="Change a DNS credential",
        response_model=DnsProviderRead,
        responses=_ok(ex.DNS_PROVIDER) | IF_MATCH_RESPONSES | VALIDATION_RESPONSE,
    )
    async def patch_dns_provider(
        provider: UUID,
        body: DnsProviderPatch,
        tenant: Tenant,
        response: Response,
        who: Who,
        svc: Services,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Change zones, settings or secrets; omitted secrets keep their stored value."""
        view = await require_dns(svc).update(
            who,
            tenant.id,
            provider,
            expected_version=_if_match(if_match),
            request_id=_rid(),
            **body.model_dump(exclude_unset=True),
        )
        response.headers["ETag"] = _etag(view.version)
        return asdict(view)

    @router.delete(
        "/tenants/{t}/dns-providers/{provider}",
        status_code=204,
        tags=["dns providers"],
        summary="Remove a DNS credential",
        responses=IF_MATCH_RESPONSES | VALIDATION_RESPONSE,
    )
    async def delete_dns_provider(
        provider: UUID,
        tenant: Tenant,
        who: Who,
        svc: Services,
        if_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """Remove a credential that no certificate names (`409` otherwise)."""
        await require_dns(svc).delete(
            who, tenant.id, provider, expected_version=_if_match(if_match), request_id=_rid()
        )
        return Response(status_code=204)

    @router.post(
        "/tenants/{t}/dns-providers/{provider}/test",
        tags=["dns providers"],
        summary="Test a DNS credential",
        response_model=DnsProviderTestRead,
        responses=_ok(ex.DNS_PROVIDER_TEST) | VALIDATION_RESPONSE,
    )
    async def test_dns_provider(
        provider: UUID,
        tenant: Tenant,
        who: Who,
        svc: Services,
        body: DnsProviderTestIn | None = None,
    ) -> dict[str, Any]:
        """Publish and remove a TXT record under `_switchyard-test.<zone>`.

        Always answers `200`; `ok: false` carries the provider's reason.
        """
        result = await require_dns(svc).test(
            who,
            tenant.id,
            provider,
            zone=body.zone if body else None,
            request_id=_rid(),
        )
        return asdict(result)

    @router.get(
        "/system/break-glass",
        tags=["break-glass"],
        summary="Break-glass login status",
        response_model=BreakGlassRead,
        responses=_ok(ex.BREAK_GLASS),
    )
    async def get_break_glass(who: Who, svc: Services) -> dict[str, Any]:
        return break_glass_json(await require_break_glass(svc).status(who))

    @router.put(
        "/system/break-glass",
        tags=["break-glass"],
        summary="Switch break-glass login on or off",
        response_model=BreakGlassRead,
        responses=_ok(ex.BREAK_GLASS) | VALIDATION_RESPONSE,
    )
    async def put_break_glass(body: BreakGlassSwitchIn, who: Who, svc: Services) -> dict[str, Any]:
        """Switch break-glass login on for a limited time, or off.

        Enabling needs a confirmed administrator and a `reason`; it lasts `ttl_minutes` (default
        240, at most 1440). Disabling has no effect while the deployment forces it on
        (`forced_by_deployment`).
        """
        status = await require_break_glass(svc).set_enabled(
            who,
            enabled=body.enabled,
            reason=body.reason,
            ttl_minutes=body.ttl_minutes,
            request_id=_rid(),
        )
        return break_glass_json(status)

    @router.post(
        "/system/break-glass/admin",
        status_code=201,
        tags=["break-glass"],
        summary="Enrol the break-glass administrator",
        response_model=BreakGlassEnrollment,
        responses=_ok(ex.BREAK_GLASS_ENROLLMENT, 201) | VALIDATION_RESPONSE,
    )
    async def create_break_glass_admin(
        who: Who, svc: Services, body: BreakGlassEnrollIn | None = None
    ) -> dict[str, Any]:
        """Generate the password and TOTP secret; they are shown once.

        Replaces any previous administrator and switches break-glass login off. The administrator
        cannot sign in until `…/admin/confirm` receives a valid code.
        """
        enrollment = await require_break_glass(svc).create_admin(
            who, username=body.username if body else "breakglass", request_id=_rid()
        )
        return {
            "username": enrollment.username,
            "password": enrollment.password,
            "totp_secret": enrollment.totp_secret,
            "otpauth_uri": enrollment.otpauth_uri,
        }

    @router.post(
        "/system/break-glass/admin/confirm",
        tags=["break-glass"],
        summary="Confirm the authenticator",
        response_model=BreakGlassRead,
        responses=_ok(ex.BREAK_GLASS) | VALIDATION_RESPONSE,
    )
    async def confirm_break_glass_admin(
        body: BreakGlassConfirmIn, who: Who, svc: Services
    ) -> dict[str, Any]:
        status = await require_break_glass(svc).confirm_admin(who, body.totp, request_id=_rid())
        return break_glass_json(status)

    @router.delete(
        "/system/break-glass/admin",
        status_code=204,
        tags=["break-glass"],
        summary="Remove the break-glass administrator",
    )
    async def delete_break_glass_admin(who: Who, svc: Services) -> Response:
        await require_break_glass(svc).delete_admin(who, request_id=_rid())
        return Response(status_code=204)

    # system tokens
    def system_token_json(r: Any) -> dict[str, Any]:
        return {
            "id": str(r.id),
            "name": r.name,
            "role": r.role.value,
            "revoked": r.revoked_at is not None,
            "expires_at": r.expires_at,
            "created_by": r.created_by,
            "created_at": r.created_at,
            "last_used_at": r.last_used_at,
        }

    @router.get(
        "/system/tokens",
        tags=["tokens"],
        summary="List system API tokens",
        response_model=list[SystemTokenRead],
        responses=_ok([ex.SYSTEM_TOKEN_READ]),
    )
    async def list_system_tokens(who: Who, svc: Services) -> list[dict[str, Any]]:
        return [system_token_json(r) for r in await svc.tokens.list_system(who)]

    @router.post(
        "/system/tokens",
        status_code=201,
        tags=["tokens"],
        summary="Create a system API token (shown once)",
        response_model=SystemTokenCreated,
        responses=_ok(ex.SYSTEM_TOKEN_CREATED, 201) | VALIDATION_RESPONSE,
    )
    async def create_system_token(body: SystemTokenIn, who: Who, svc: Services) -> dict[str, Any]:
        """A `system-admin` token for bootstrap and controller-level automation.

        30 days by default, 90 at most. A token cannot create tokens, so this needs a signed-in
        person.
        """
        from datetime import timedelta

        plaintext, record = await svc.tokens.create_system(
            who, body.name, ttl=timedelta(days=body.ttl_days), request_id=_rid()
        )
        return {
            "id": str(record.id),
            "token": plaintext,
            "role": record.role.value,
            "expires_at": record.expires_at,
        }

    @router.delete(
        "/system/tokens/{token}",
        status_code=204,
        tags=["tokens"],
        summary="Revoke a system API token",
    )
    async def revoke_system_token(token: UUID, who: Who, svc: Services) -> Response:
        await svc.tokens.revoke_system(who, token, request_id=_rid())
        return Response(status_code=204)

    # role mappings
    @router.get(
        "/role-mappings",
        tags=["access"],
        summary="List role mappings (system-wide)",
        response_model=list[RoleMappingRead],
        responses=_ok([ex.ROLE_MAPPING]),
    )
    async def list_role_mappings(
        who: Who,
        svc: Services,
        provider: str | None = None,
        tenant: str | None = None,
        group: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every mapping, optionally filtered by provider (id or name), tenant or group."""
        tenant_id = (await svc.tenants.resolve(who, tenant)).id if tenant else None
        rows = await svc.role_mappings.list(
            who, provider=provider, group=group, tenant_filter=tenant_id
        )
        return [_mapping_json(r) for r in rows]

    @router.post(
        "/role-mappings",
        status_code=201,
        tags=["access"],
        summary="Create a role mapping (system-wide)",
        response_model=RoleMappingRead,
        responses=_ok(ex.ROLE_MAPPING, 201) | VALIDATION_RESPONSE,
    )
    async def create_role_mapping(
        body: SystemRoleMappingIn, who: Who, svc: Services
    ) -> dict[str, Any]:
        record = await svc.role_mappings.create(
            who,
            provider=body.provider,
            group_name=body.group_name,
            role=body.role,
            tenant_id=body.tenant_id,
            label=body.label,
            request_id=_rid(),
        )
        return _mapping_json(record)

    @router.get(
        "/role-mappings/{mapping}",
        tags=["access"],
        summary="Get a role mapping",
        response_model=RoleMappingRead,
        responses=_ok(ex.ROLE_MAPPING, headers=ETAG_HEADER),
    )
    async def get_role_mapping(
        mapping: UUID, who: Who, svc: Services, response: Response
    ) -> dict[str, Any]:
        record = await svc.role_mappings.get(who, mapping)
        response.headers["ETag"] = _etag(record.version)
        return _mapping_json(record)

    @router.patch(
        "/role-mappings/{mapping}",
        tags=["access"],
        summary="Change a role mapping",
        response_model=RoleMappingRead,
        responses=_ok(ex.ROLE_MAPPING) | IF_MATCH_RESPONSES | VALIDATION_RESPONSE,
    )
    async def patch_role_mapping(
        mapping: UUID,
        body: RoleMappingPatch,
        who: Who,
        svc: Services,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        record = await svc.role_mappings.update(
            who,
            mapping,
            expected_version=_if_match(if_match),
            changes=body.model_dump(exclude_unset=True, mode="json"),
            request_id=_rid(),
        )
        response.headers["ETag"] = _etag(record.version)
        return _mapping_json(record)

    @router.delete(
        "/role-mappings/{mapping}",
        status_code=204,
        tags=["access"],
        summary="Delete a role mapping",
    )
    async def delete_role_mapping(mapping: UUID, who: Who, svc: Services) -> Response:
        await svc.role_mappings.delete(who, mapping, request_id=_rid())
        return Response(status_code=204)

    @router.get(
        "/tenants/{t}/role-mappings",
        tags=["access"],
        summary="List a tenant's role mappings",
        response_model=list[RoleMappingRead],
        responses=_ok([ex.ROLE_MAPPING]),
    )
    async def list_tenant_role_mappings(
        tenant: Tenant,
        who: Who,
        svc: Services,
        provider: str | None = None,
        group: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = await svc.role_mappings.list(
            who, tenant_id=tenant.id, provider=provider, group=group
        )
        return [_mapping_json(r) for r in rows]

    @router.post(
        "/tenants/{t}/role-mappings",
        status_code=201,
        tags=["access"],
        summary="Create a role mapping in a tenant",
        response_model=RoleMappingRead,
        responses=_ok(ex.ROLE_MAPPING, 201) | VALIDATION_RESPONSE,
    )
    async def create_tenant_role_mapping(
        body: RoleMappingIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        """A tenant administrator may map `viewer` and `operator` here, nothing stronger."""
        record = await svc.role_mappings.create(
            who,
            provider=body.provider,
            group_name=body.group_name,
            role=body.role,
            tenant_id=tenant.id,
            label=body.label,
            request_id=_rid(),
        )
        return _mapping_json(record)

    @router.patch(
        "/tenants/{t}/role-mappings/{mapping}",
        tags=["access"],
        summary="Change a role mapping in a tenant",
        response_model=RoleMappingRead,
        responses=_ok(ex.ROLE_MAPPING) | IF_MATCH_RESPONSES | VALIDATION_RESPONSE,
    )
    async def patch_tenant_role_mapping(
        mapping: UUID,
        body: RoleMappingPatch,
        tenant: Tenant,
        who: Who,
        svc: Services,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        record = await svc.role_mappings.update(
            who,
            mapping,
            expected_version=_if_match(if_match),
            changes=body.model_dump(exclude_unset=True, mode="json"),
            tenant_id=tenant.id,
            request_id=_rid(),
        )
        response.headers["ETag"] = _etag(record.version)
        return _mapping_json(record)

    @router.delete(
        "/tenants/{t}/role-mappings/{mapping}",
        status_code=204,
        tags=["access"],
        summary="Delete a role mapping in a tenant",
    )
    async def delete_tenant_role_mapping(
        mapping: UUID, tenant: Tenant, who: Who, svc: Services
    ) -> Response:
        await svc.role_mappings.delete(who, mapping, tenant_id=tenant.id, request_id=_rid())
        return Response(status_code=204)

    # users
    @router.get(
        "/users",
        tags=["access"],
        summary="List users",
        response_model=Page[UserRead],
        responses=_ok({"items": [ex.USER_READ], "next_cursor": None}),
    )
    async def list_users(
        who: Who,
        svc: Services,
        q: str | None = None,
        provider: str | None = None,
        tenant: str | None = None,
        disabled: bool | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """People who have signed in, newest sign-in first. `q` searches name and e-mail."""
        tenant_id = (await svc.tenants.resolve(who, tenant)).id if tenant else None
        page = await svc.users.list_users(
            who,
            q=q,
            provider=provider,
            tenant_filter=tenant_id,
            disabled=disabled,
            cursor=cursor,
            limit=limit,
        )
        return {"items": [_user_json(u) for u in page.items], "next_cursor": page.next_cursor}

    @router.get(
        "/users/{user}",
        tags=["access"],
        summary="Get a user",
        response_model=UserDetail,
        responses=_ok(ex.USER_DETAIL),
    )
    async def get_user(user: UUID, who: Who, svc: Services) -> dict[str, Any]:
        """A user with what they can do now (`grants`, with the groups that gave it)."""
        return _user_json(await svc.users.get(who, user), detail=True)

    @router.post(
        "/users/{user}/disable",
        tags=["access"],
        summary="Disable a user",
        response_model=UserDetail,
        responses=_ok(ex.USER_DETAIL),
    )
    async def disable_user(user: UUID, who: Who, svc: Services) -> dict[str, Any]:
        """Refuse the person at sign-in and end every session of theirs at once."""
        record = await svc.users.set_disabled(who, user, True, request_id=_rid())
        return _user_json(record, detail=True)

    @router.post(
        "/users/{user}/enable",
        tags=["access"],
        summary="Enable a user",
        response_model=UserDetail,
        responses=_ok(ex.USER_DETAIL),
    )
    async def enable_user(user: UUID, who: Who, svc: Services) -> dict[str, Any]:
        record = await svc.users.set_disabled(who, user, False, request_id=_rid())
        return _user_json(record, detail=True)

    @router.post(
        "/users/{user}/revoke-sessions",
        tags=["access"],
        summary="End a user's sessions",
        response_model=SessionsRevoked,
        responses=_ok(ex.SESSIONS_REVOKED),
    )
    async def revoke_user_sessions(user: UUID, who: Who, svc: Services) -> dict[str, Any]:
        return {"revoked": await svc.users.revoke_sessions(who, user, request_id=_rid())}

    @router.delete("/users/{user}", status_code=204, tags=["access"], summary="Delete a user")
    async def delete_user(user: UUID, who: Who, svc: Services) -> Response:
        """Remove the record and its sessions; the person reappears at their next sign-in."""
        await svc.users.delete(who, user, request_id=_rid())
        return Response(status_code=204)

    @router.get(
        "/tenants/{t}/users",
        tags=["access"],
        summary="List people with access to a tenant",
        response_model=Page[UserRead],
        responses=_ok({"items": [ex.USER_READ], "next_cursor": None}),
    )
    async def list_tenant_users(
        tenant: Tenant,
        who: Who,
        svc: Services,
        q: str | None = None,
        provider: str | None = None,
        disabled: bool | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """People with a grant in this tenant, showing only this tenant's grants."""
        page = await svc.users.list_users(
            who,
            tenant_id=tenant.id,
            q=q,
            provider=provider,
            disabled=disabled,
            cursor=cursor,
            limit=limit,
        )
        return {"items": [_user_json(u) for u in page.items], "next_cursor": page.next_cursor}

    # agent enrollment
    @router.post(
        "/tenants/{t}/engines/{engine}/enrollment-token",
        status_code=201,
        tags=["engines"],
        summary="Create an agent enrollment token",
        response_model=EnrollmentToken,
        responses=_ok(ex.ENROLLMENT_TOKEN, 201),
    )
    async def enrollment_token(
        engine: UUID, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        """Create a one-time token (shown once) for ``sy-agent enroll``."""
        if svc.enrollment is None:
            raise ApiError(503, "enrollment is not configured on this controller")
        token, expires = await svc.enrollment.create_token(
            who, tenant.id, engine, request_id=_rid()
        )
        return {"token": token, "expires_at": expires}

    @router.post(
        "/enroll",
        tags=["engines"],
        summary="Trade an enrollment token and CSR for a certificate",
        response_model=EnrollOut,
        responses=_ok(ex.ENROLL_OUT, problems=False),
    )
    async def enroll(body: EnrollIn, svc: Services) -> dict[str, Any]:
        """Trade an enrollment or renewal token and a CSR for the agent's certificate."""
        if svc.enrollment is None:
            raise ApiError(503, "enrollment is not configured on this controller")
        try:
            result = await svc.enrollment.redeem(body.token, body.csr)
        except EnrollmentError as exc:
            raise ApiError(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc
        except PkiError as exc:
            raise ApiError(422, str(exc)) from exc
        return {
            "engine": result.engine,
            "certificate": result.cert_pem,
            "ca": result.ca_pem,
            "bundle_public_key": result.bundle_public_key,
            "renewal_token": result.renewal_token,
            "not_after": result.not_after,
        }

    # certificates
    @router.post(
        "/tenants/{t}/certificates/import",
        status_code=202,
        tags=["certificates"],
        summary="Import a certificate and key",
        response_model=Accepted,
        responses=_ok(ex.ACCEPTED, 202) | VALIDATION_RESPONSE,
    )
    async def import_certificate(
        body: CertImportIn,
        tenant: Tenant,
        who: Who,
        svc: Services,
        change_set: UUID | None = None,
        apply: bool = False,
    ) -> Accepted:
        """Store an uploaded certificate and key (PEM or PKCS#12) and add it to a change set."""
        if svc.certs is None:
            raise ApiError(503, "no secrets backend is configured for certificates")
        if body.pkcs12_b64 is not None:
            data = await svc.certs.import_pkcs12(
                who,
                tenant.id,
                body.name,
                body.pkcs12_b64,
                body.password.get_secret_value() if body.password else None,
            )
        else:
            pem = body.pem or ""
            data = await svc.certs.import_pem(who, tenant.id, body.name, pem)
        op = ChangeOp(
            object_type=ObjectType.CERTIFICATE, object_id=uuid7(), action=Action.CREATE, body=data
        )
        return await _write(
            svc, who, tenant, op, change_set, apply, f"import certificate {body.name}"
        )

    @router.post(
        "/tenants/{t}/certificates/import-ca",
        status_code=202,
        tags=["certificates"],
        summary="Import a client CA for mutual TLS",
        description=(
            "Stores CA certificates (no key) and an optional revocation list, and adds a "
            "`certificate` with `purpose: client_ca` to a change set. Reference it from a TLS "
            "port as `client_ca_id` (with `client_verify` `required` or `optional`) to check "
            "client certificates. It cannot be used as a server certificate."
        ),
        response_model=Accepted,
        responses=_ok(ex.ACCEPTED, 202) | VALIDATION_RESPONSE,
    )
    async def import_client_ca(
        body: CaImportIn,
        tenant: Tenant,
        who: Who,
        svc: Services,
        change_set: UUID | None = None,
        apply: bool = False,
    ) -> Accepted:
        if svc.certs is None:
            raise ApiError(503, "no secrets backend is configured for certificates")
        data = await svc.certs.import_client_ca(who, tenant.id, body.name, body.pem, body.crl_pem)
        op = ChangeOp(
            object_type=ObjectType.CERTIFICATE, object_id=uuid7(), action=Action.CREATE, body=data
        )
        return await _write(
            svc, who, tenant, op, change_set, apply, f"import client CA {body.name}"
        )

    @router.post(
        "/tenants/{t}/certificates/issue",
        status_code=202,
        tags=["certificates"],
        summary="Issue a certificate (ACME or Cloudflare Origin CA)",
        response_model=JobAccepted,
        responses=_ok(ex.JOB_ACCEPTED, 202) | VALIDATION_RESPONSE,
    )
    async def issue_certificate(
        body: CertIssueIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, str]:
        """Queue issuance through ACME DNS-01 or Cloudflare Origin CA."""
        job = await svc.cert_jobs.request_issue(
            who,
            tenant.id,
            name=body.name,
            source=CertSource(body.source),
            sans=tuple(body.sans),
            acme_provider=body.acme_provider,
            challenge=AcmeChallenge(body.challenge) if body.challenge else None,
            dns_provider=body.dns_provider,
            request_id=_rid(),
        )
        return {"job_id": str(job)}

    @router.post(
        "/tenants/{t}/certificates/{certificate}/replace",
        status_code=202,
        tags=["certificates"],
        summary="Replace a certificate's material, keeping its id and usages",
        response_model=Accepted,
        responses=_ok(ex.ACCEPTED, 202) | VALIDATION_RESPONSE,
    )
    async def replace_certificate(
        certificate: UUID,
        body: CertReplaceIn,
        tenant: Tenant,
        who: Who,
        svc: Services,
        change_set: UUID | None = None,
        apply: bool = False,
    ) -> Accepted:
        """Store new certificate and key (PEM or PKCS#12) and update the object in a change set."""
        if svc.certs is None:
            raise ApiError(503, "no secrets backend is configured for certificates")
        data, version = await svc.certs.replace(
            who,
            tenant.id,
            certificate,
            pem=body.pem,
            pkcs12_b64=body.pkcs12_b64,
            password=body.password.get_secret_value() if body.password else None,
        )
        op = ChangeOp(
            object_type=ObjectType.CERTIFICATE,
            object_id=certificate,
            action=Action.UPDATE,
            body=data,
            expected_version=version,
        )
        return await _write(svc, who, tenant, op, change_set, apply, "replace certificate")

    @router.post(
        "/tenants/{t}/certificates/{certificate}/renew",
        status_code=202,
        tags=["certificates"],
        summary="Renew a certificate",
        response_model=JobAccepted,
        responses=_ok(ex.JOB_ACCEPTED, 202),
    )
    async def renew_certificate(
        certificate: UUID, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, str]:
        """Queue a renewal of one certificate."""
        job = await svc.cert_jobs.request_renew(who, tenant.id, certificate, request_id=_rid())
        return {"job_id": str(job)}

    # infrastructure jobs
    @router.get(
        "/tenants/{t}/sdn/status",
        tags=["infrastructure"],
        summary="SDN apply state of every managed network",
        description=(
            "For the 'SDN apply running' chip: per managed network, whether a tenant-wide "
            "`apply_sdn` job is `queued` or `running`, or how the latest finished one ended "
            "(`succeeded`, `failed`, or `never` for a network it did not include). Requires any "
            "role in the tenant."
        ),
        response_model=list[SdnApplyRead],
        responses=_ok(ex.SDN_STATUS),
    )
    async def sdn_status(tenant: Tenant, who: Who, svc: Services) -> list[dict[str, Any]]:
        return [s.__dict__ for s in await svc.infra.sdn_status(who, tenant.id)]

    @router.post(
        "/tenants/{t}/networks/apply-sdn",
        status_code=202,
        tags=["infrastructure"],
        summary="Apply the Proxmox SDN objects of managed networks",
        response_model=JobAccepted,
        responses=_ok(ex.JOB_ACCEPTED, 202),
    )
    async def apply_sdn(tenant: Tenant, who: Who, svc: Services) -> dict[str, str]:
        """Create or update the Proxmox SDN objects of the tenant's managed networks."""
        job = await svc.infra.request_sdn_apply(who, tenant.id, request_id=_rid())
        return {"job_id": str(job)}

    @router.post(
        "/tenants/{t}/engines/{engine}/retire",
        status_code=202,
        tags=["infrastructure"],
        summary="Retire an engine and destroy its VM",
        response_model=JobAccepted,
        responses=_ok(ex.JOB_ACCEPTED, 202),
    )
    async def retire_engine(
        engine: UUID, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, str]:
        """Disable an engine, destroy its VM and remove it (refused for the last serving engine)."""
        job = await svc.infra.request_retire(who, tenant.id, engine, request_id=_rid())
        return {"job_id": str(job)}

    @router.post(
        "/tenants/{t}/se-groups/{group}/deploy",
        status_code=202,
        tags=["infrastructure"],
        summary="Deploy engines for an SE group on Proxmox",
        response_model=JobAccepted,
        responses=_ok(ex.JOB_ACCEPTED, 202) | VALIDATION_RESPONSE,
    )
    async def deploy_group(
        group: UUID, body: DeployIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, str]:
        """Grow an SE group to ``count`` engines by cloning the SE template on Proxmox.

        `403` with code `lxc_disabled` when `guest_kind` is `lxc` and containers are not enabled.
        """
        if body.guest_kind == "lxc" and not await svc.settings.lxc_allowed():
            raise ApiError(403, LXC_DISABLED, code="lxc_disabled")
        request = DeployRequest(
            tenant_id=tenant.id,
            se_group_id=group,
            count=body.count,
            nodes=tuple(body.nodes),
            template_vmid=body.template_vmid,
            mgmt_pool_id=body.mgmt_pool_id,
            pool=body.pool,
            credentials_ref=body.credentials_ref,
            ssh_public_keys=body.ssh_public_keys,
            guest_kind=body.guest_kind,
            nic_pools=body.nic_pools or {},
        )
        job = await svc.infra.request_deploy(who, tenant.id, request, request_id=_rid())
        return {"job_id": str(job)}

    # live status
    def status_json(s: EngineStatus, who: Principal) -> dict[str, Any]:
        # Raw connection errors name addresses and TLS details; tenants get a fixed sentence.
        error = s.error if who.is_system or not s.error else GENERIC_ENGINE_ERROR
        return {
            "engine": s.name,
            "engine_id": str(s.engine_id),
            "se_group_id": str(s.se_group_id),
            "reachable": s.reachable,
            "error": error,
            "haproxy_fingerprint": s.haproxy_fingerprint,
            "members": s.members,
            "agent": s.agent,
            "drift": s.drift,
            "observed_at": s.observed_at,
        }

    def vs_status_json(v: VirtualServiceStatus) -> dict[str, Any]:
        return {
            "id": v.id,
            "name": v.name,
            "protocol": v.protocol,
            "state": v.state,
            "health": v.health,
            "members_up": v.members_up,
            "members_total": v.members_total,
            "members": [m.__dict__ for m in v.members],
        }

    @router.get(
        "/tenants/{t}/status/engines",
        tags=["status"],
        summary="Last observed state of every engine",
        response_model=list[EngineStatusRead],
        responses=_ok([ex.ENGINE_STATUS]),
    )
    async def engine_status(tenant: Tenant, who: Who, svc: Services) -> list[dict[str, Any]]:
        return [status_json(s, who) for s in await svc.status.list_status(who, tenant.id)]

    @router.get(
        "/tenants/{t}/status/virtual-services",
        tags=["status"],
        summary="State of every virtual service",
        response_model=list[VirtualServiceStatusRead],
        responses=_ok([ex.VIRTUAL_SERVICE_STATUS]),
    )
    async def virtual_service_status(
        tenant: Tenant, who: Who, svc: Services
    ) -> list[dict[str, Any]]:
        """UDP services are `serving` (IPVS); `not_served` is reserved for unrenderable ones."""
        return [vs_status_json(v) for v in await svc.status.virtual_services(who, tenant.id)]

    @router.get(
        "/tenants/{t}/status/stream",
        tags=["status"],
        summary="Live events (server-sent events)",
        description=(
            "A `text/event-stream` of what changes in the tenant. Events: "
            + "; ".join(f"`{n}`: {d}" for n, (_, _, d) in EVENTS.items())
            + " Every `data:` line is JSON (the payload models are under `x-sse-events`; "
            "`status` and `vs_state` carry an array). The first events are a full snapshot, "
            "so a client that reconnects simply starts over (`Last-Event-ID` is not used). "
            "Comment lines `: keep-alive` fill the quiet intervals. The database is polled every "
            "`interval` seconds; `follow=false` sends one snapshot and closes."
        ),
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "`text/event-stream`.",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            }
        },
    )
    async def status_stream(
        request: Request,
        tenant: Tenant,
        who: Who,
        svc: Services,
        follow: bool = True,
        interval: Annotated[float, Query(ge=0.1, le=60)] = 2.0,
    ) -> StreamingResponse:
        import asyncio
        import json

        def frame(counter: int, name: str, data: Any) -> str:
            return f"id: {counter}\nevent: {name}\ndata: {json.dumps(data, sort_keys=True)}\n\n"

        async def events() -> AsyncIterator[str]:
            counter = 0
            last: dict[str, str] = {}
            jobs_seen: dict[UUID, str] = {}
            revision: int | None = None
            first = True
            while True:
                sent = False
                engines = [
                    EngineStatusRead.model_validate(status_json(s, who)).model_dump(mode="json")
                    for s in await svc.status.list_status(who, tenant.id)
                ]
                states = [
                    VirtualServiceStatusRead.model_validate(vs_status_json(v)).model_dump(
                        mode="json"
                    )
                    for v in await svc.status.virtual_services(who, tenant.id)
                ]
                for name, payload in (("status", engines), ("vs_state", states)):
                    text = json.dumps(payload, sort_keys=True)
                    if last.get(name) != text:
                        counter += 1
                        last[name] = text
                        sent = True
                        yield frame(counter, name, payload)
                page = await svc.jobs.list(who, tenant.id, limit=20)
                for row in reversed(page.items):
                    job = job_models.Job.from_row(row).model_dump(mode="json")
                    text = json.dumps(job, sort_keys=True)
                    if jobs_seen.get(row.id) == text:
                        continue
                    unfinished = job["state"] in {"queued", "running"}
                    if not first or unfinished:
                        counter += 1
                        sent = True
                        yield frame(counter, "job_progress", job)
                    jobs_seen[row.id] = text
                latest = await svc.change_sets.revisions(who, tenant.id, limit=1)
                if latest and latest[0].number != revision:
                    if revision is not None:
                        counter += 1
                        sent = True
                        r = latest[0]
                        yield frame(
                            counter,
                            "revision_applied",
                            RevisionSummary(
                                number=r.number,
                                author=r.author,
                                summary=r.summary,
                                git_sha=r.git_sha,
                                created_at=r.created_at,
                            ).model_dump(mode="json"),
                        )
                    revision = latest[0].number
                first = False
                if not sent:
                    yield ": keep-alive\n\n"
                if not follow or await request.is_disconnected():
                    return
                await asyncio.sleep(interval)

        return StreamingResponse(events(), media_type="text/event-stream")

    # jobs and audit
    @router.get(
        "/jobs/{job_id}",
        tags=["jobs"],
        summary="Get a background job",
        response_model=JobRead,
        responses=_ok(ex.JOB_READ),
    )
    async def get_job(job_id: UUID, who: Who, svc: Services) -> dict[str, Any]:
        from sqlalchemy import select

        from switchyard.db.ops_models import JobRow

        ctx = (
            admin_session(svc.db.admin)
            if who.is_system
            else tenant_session(svc.db.app, sorted(who.tenant_ids))
        )
        async with ctx as session:
            row = (
                await session.execute(select(JobRow).where(JobRow.id == job_id))
            ).scalar_one_or_none()
        if row is None:
            raise ApiError(404, "job not found")
        return {
            "id": str(row.id),
            "kind": row.kind,
            "state": row.state,
            "attempts": row.attempts,
            "error": row.last_error,
            "result": row.result,
        }

    @router.get(
        "/tenants/{t}/jobs",
        tags=["jobs"],
        summary="List a tenant's jobs, newest first",
        response_model=job_models.Page[job_models.Job],
        responses=_ok({"items": [ex.JOB_FULL], "next_cursor": None}),
    )
    async def list_jobs(
        tenant: Tenant,
        who: Who,
        svc: Services,
        state: JobState | None = None,
        kind: job_models.JobKind | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Any:
        try:
            page = await svc.jobs.list(
                who, tenant.id, state=state, kind=kind, cursor=cursor, limit=limit
            )
        except ValueError:
            raise ApiError(400, "cursor is not valid") from None
        return job_models.Page[job_models.Job](
            items=[job_models.Job.from_row(r) for r in page.items], next_cursor=page.next_cursor
        )

    @router.get(
        "/tenants/{t}/jobs/{job_id}",
        tags=["jobs"],
        summary="Get a job with its stages",
        response_model=job_models.Job,
        responses=_ok(ex.JOB_FULL),
    )
    async def get_tenant_job(job_id: UUID, tenant: Tenant, who: Who, svc: Services) -> Any:
        return job_models.Job.from_row(await svc.jobs.get(who, tenant.id, job_id))

    @router.get(
        "/tenants/{t}/jobs/{job_id}/log",
        tags=["jobs"],
        summary="Get a job's log",
        response_model=job_models.JobLog,
        responses=_ok(ex.JOB_LOG),
    )
    async def get_job_log(job_id: UUID, tenant: Tenant, who: Who, svc: Services) -> Any:
        row = await svc.jobs.get(who, tenant.id, job_id)
        return job_models.JobLog(
            job_id=row.id, lines=[job_models.LogLine.model_validate(x) for x in row.log]
        )

    @router.post(
        "/tenants/{t}/jobs/{job_id}/retry",
        tags=["jobs"],
        summary="Queue a dead job again",
        response_model=job_models.Job,
        responses=_ok(ex.JOB_FULL),
    )
    async def retry_job(job_id: UUID, tenant: Tenant, who: Who, svc: Services) -> Any:
        return job_models.Job.from_row(
            await svc.jobs.retry(who, tenant.id, job_id, request_id=_rid())
        )

    @router.post(
        "/tenants/{t}/jobs/{job_id}/cancel",
        tags=["jobs"],
        summary="Cancel a job that has not started",
        response_model=job_models.Job,
        responses=_ok(ex.JOB_FULL),
    )
    async def cancel_job(job_id: UUID, tenant: Tenant, who: Who, svc: Services) -> Any:
        return job_models.Job.from_row(
            await svc.jobs.cancel(who, tenant.id, job_id, request_id=_rid())
        )

    @router.get(
        "/tenants/{t}/audit-events",
        tags=["audit"],
        summary="List audit events, newest first",
        description=AUDIT_DESCRIPTION + " Requires any role in the tenant.",
        response_model=Page[AuditEventRead],
        responses=_ok({"items": [ex.AUDIT_EVENT], "next_cursor": None}),
    )
    async def audit_events(
        tenant: Tenant,
        who: Who,
        svc: Services,
        filters: Annotated[AuditFilters, Depends()],
    ) -> Page[Any]:
        async with tenant_session(svc.db.app, [tenant.id]) as session:
            return await _audit_page(session, filters)

    @router.get(
        "/audit-events",
        tags=["audit"],
        summary="List audit events across tenants (system-admin)",
        description=(
            AUDIT_DESCRIPTION
            + " This route spans every tenant and the system-level events that belong to none "
            "(sign-ins, tenant creation); narrow it with `tenant` (id or name). Requires "
            "`system-admin`."
        ),
        response_model=Page[AuditEventRead],
        responses=_ok({"items": [ex.AUDIT_EVENT], "next_cursor": None}),
    )
    async def system_audit_events(
        who: Who,
        svc: Services,
        filters: Annotated[AuditFilters, Depends()],
        tenant: Annotated[str | None, Query(description="Tenant id or name.")] = None,
    ) -> Page[Any]:
        require(who, "manage", "tenant")
        if tenant:
            filters.tenant_id = (await svc.tenants.resolve(who, tenant)).id
        async with admin_session(svc.db.admin) as session:
            return await _audit_page(session, filters)

    actor_query = Query(
        description="A principal label to resolve (repeat up to 100 times).", max_length=100
    )

    @router.get(
        "/tenants/{t}/principals",
        tags=["audit"],
        summary="Names for principal labels",
        description=(
            "Turns the labels that audit events, change sets and revisions carry (`user:...`, "
            "`token:...`, `local:...`, `system:...`) into names for people, in the order given, "
            "one answer per distinct label. Users who appear in this tenant's own history get the "
            "name their identity provider gave; any other `user:` label is answered from the "
            "label alone. Requires any role in the tenant."
        ),
        response_model=list[PrincipalNameRead],
        responses=_ok(ex.PRINCIPALS),
    )
    async def tenant_principals(
        tenant: Tenant,
        who: Who,
        svc: Services,
        actor: Annotated[list[str] | None, actor_query] = None,
    ) -> list[dict[str, str]]:
        try:
            names = await principals.display_names(svc.db, actor or [], tenant_id=tenant.id)
        except ValueError as exc:
            raise ApiError(422, str(exc)) from None
        return [n.__dict__ for n in names]

    @router.get(
        "/principals",
        tags=["audit"],
        summary="Names for principal labels (system-admin)",
        description=(
            "As `GET /tenants/{t}/principals` for any user, for the cross-tenant audit log. "
            "Requires `system-admin`."
        ),
        response_model=list[PrincipalNameRead],
        responses=_ok(ex.PRINCIPALS),
    )
    async def system_principals(
        who: Who, svc: Services, actor: Annotated[list[str] | None, actor_query] = None
    ) -> list[dict[str, str]]:
        require(who, "manage", "tenant")
        try:
            names = await principals.display_names(svc.db, actor or [])
        except ValueError as exc:
            raise ApiError(422, str(exc)) from None
        return [n.__dict__ for n in names]

    @router.get(
        "/tenants/{t}/audit-events/export",
        tags=["audit"],
        summary="Download audit events as CSV or JSON Lines",
        description=(
            "The same filters as the list (`actor`, `action`, `object`, `since`, `until`, `q`; "
            "`cursor` and `limit` are ignored) but every matching event, newest first, streamed "
            "as a file. `format` is `csv` (default) or `jsonl`. The export itself is audited as "
            "`audit.export`. Requires any role in the tenant, like the list."
        ),
        response_class=StreamingResponse,
        responses=EXPORT_RESPONSES,
    )
    async def export_audit_events(
        tenant: Tenant,
        who: Who,
        svc: Services,
        filters: Annotated[AuditFilters, Depends()],
        format: Annotated[str, Query(description="`csv` or `jsonl`.")] = "csv",  # noqa: A002
    ) -> StreamingResponse:
        filters.tenant_id = tenant.id
        async with tenant_session(svc.db.app, [tenant.id]) as session:
            await audit_log.record(
                session,
                tenant_id=tenant.id,
                actor=who.actor,
                action="audit.export",
                diff=_export_diff(filters, format),
                request_id=_rid(),
            )
        return _export_response(
            lambda: tenant_session(svc.db.app, [tenant.id]), filters, format, f"audit-{tenant.name}"
        )

    @router.get(
        "/audit-events/export",
        tags=["audit"],
        summary="Download audit events across tenants (system-admin)",
        description=(
            "As `GET /tenants/{t}/audit-events/export` over every tenant and the system-level "
            "events; narrow it with `tenant` (id or name). Requires `system-admin`."
        ),
        response_class=StreamingResponse,
        responses=EXPORT_RESPONSES,
    )
    async def export_system_audit_events(
        who: Who,
        svc: Services,
        filters: Annotated[AuditFilters, Depends()],
        tenant: Annotated[str | None, Query(description="Tenant id or name.")] = None,
        format: Annotated[str, Query(description="`csv` or `jsonl`.")] = "csv",  # noqa: A002
    ) -> StreamingResponse:
        require(who, "manage", "tenant")
        if tenant:
            filters.tenant_id = (await svc.tenants.resolve(who, tenant)).id
        async with admin_session(svc.db.admin) as session:
            await audit_log.record(
                session,
                tenant_id=filters.tenant_id,
                actor=who.actor,
                action="audit.export",
                diff=_export_diff(filters, format),
                request_id=_rid(),
            )
        return _export_response(lambda: admin_session(svc.db.admin), filters, format, "audit-all")

    @router.get(
        "/audit-actions",
        tags=["audit"],
        summary="Catalog of audit event actions",
        description=(
            "Every `action` an audit event can carry, with its category (the part before the "
            "dot) and a one-sentence description, for labels and filters. Any authenticated "
            "caller."
        ),
        response_model=list[AuditActionRead],
        responses=_ok([ex.AUDIT_ACTION]),
    )
    async def audit_actions(who: Who) -> list[AuditActionRead]:
        return [
            AuditActionRead(action=a.action, category=a.category, description=a.description)
            for a in CATALOG
        ]

    app.include_router(router)
    app.openapi = lambda: _problem_json_openapi(app)  # type: ignore[method-assign]
    return app

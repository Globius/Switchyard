"""The FastAPI application: ``/api/v1``.

Authentication is a bearer API token (see :mod:`switchyard.services.tokens`); OIDC/SAML login will
issue short-lived sessions in front of the same principal model. Errors are RFC 9457 problem+json.
Writes to desired state always go through a change set: ``?change_set=<id>`` adds an operation to a
draft, ``?apply=true`` opens an implicit single-operation change set and applies it.
"""

import hmac
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from switchyard.adapters.pki import PkiError
from switchyard.adapters.secrets import SecretsReader
from switchyard.api import examples as ex
from switchyard.api import job_models
from switchyard.api.problems import ApiError, install_handlers
from switchyard.api.rendered_models import RenderedConfig, RenderedFragment
from switchyard.api.schemas import (
    ETAG_HEADER,
    IF_MATCH_RESPONSES,
    OBJECT_APIS,
    PROBLEM_RESPONSES,
    VALIDATION_RESPONSE,
    Accepted,
    ApplyIn,
    AuditEventRead,
    AuthProviders,
    BreakGlassIn,
    CertImportIn,
    CertIssueIn,
    ChangeSetDiff,
    ChangeSetRead,
    ChangeSetSummary,
    DeployIn,
    EngineIn,
    EngineRead,
    EngineStateOut,
    EngineStatusRead,
    EnrollIn,
    EnrollmentToken,
    EnrollOut,
    Health,
    JobAccepted,
    JobRead,
    Me,
    MemberStateIn,
    MemberStateResult,
    OpenIn,
    OpsIn,
    Page,
    ProtocolRulesRead,
    ReasonIn,
    RevisionRead,
    RevisionSummary,
    StateOut,
    TenantIn,
    TenantPatch,
    TenantRead,
    TokenCreated,
    TokenIn,
    TokenRead,
    VirtualServiceStatusRead,
)
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.changes import (
    COLLECTION,
    Action,
    ChangeOp,
    DraftStatus,
    ObjectType,
)
from switchyard.domain.field_rules import protocol_rules
from switchyard.domain.models import CertSource, Protocol
from switchyard.domain.workflow import ChangeSetState, EngineState, JobState
from switchyard.ids import uuid7
from switchyard.pipeline.engines import EngineDirectory
from switchyard.render.escape import RenderError
from switchyard.render.fragments import RENDERED_TYPES, rendered_fragments
from switchyard.services.certificates import CertificateJobs, CertificateService
from switchyard.services.changesets import ChangeSetDetail, ChangeSetService
from switchyard.services.context import Databases, Principal
from switchyard.services.engines import EngineService
from switchyard.services.enrollment import EnrollmentError, EnrollmentService
from switchyard.services.infra import DeployRequest, InfraJobs
from switchyard.services.jobs import JobService
from switchyard.services.members import MemberService
from switchyard.services.sessions import LoginError, SessionInfo, SessionService
from switchyard.services.status import EngineStatus, StatusService
from switchyard.services.tenants import TenantRecord, TenantService
from switchyard.services.tokens import TokenService

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
    certs: CertificateService | None = None
    enrollment: EnrollmentService | None = None
    sessions: SessionService | None = None


def build_services(
    db: Databases,
    directory: EngineDirectory,
    secrets: SecretsReader | None = None,
    certs: CertificateService | None = None,
    enrollment: EnrollmentService | None = None,
    sessions: SessionService | None = None,
) -> ApiServices:
    """Wire the services over one set of database handles."""
    return ApiServices(
        db=db,
        tenants=TenantService(db),
        tokens=TokenService(db),
        change_sets=ChangeSetService(db),
        engines=EngineService(db),
        members=MemberService(db, directory),
        status=StatusService(db, directory, secrets),
        infra=InfraJobs(db),
        cert_jobs=CertificateJobs(db),
        jobs=JobService(db),
        certs=certs,
        enrollment=enrollment,
        sessions=sessions,
    )


# -- dependencies ---------------------------------------------------------------------------------


def _services(request: Request) -> ApiServices:
    services: ApiServices = request.app.state.services
    return services


Services = Annotated[ApiServices, Depends(_services)]


SESSION_COOKIE = "sy_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def current_identity(
    request: Request, services: Services, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    """Authenticate the request: a bearer API token, or the session cookie plus a CSRF header.

    Bearer tokens are for machines and are not subject to CSRF. A cookie session must send
    ``X-CSRF-Token`` (the value from ``GET /me``) on every request that changes something.
    """
    headers = {"WWW-Authenticate": "Bearer"}
    if authorization and authorization.lower().startswith("bearer "):
        principal = await services.tokens.authenticate(authorization[7:].strip())
        if principal is None:
            raise ApiError(401, "invalid, expired or revoked token", headers=headers)
        return principal
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and services.sessions is not None:
        info = await services.sessions.authenticate(cookie)
        if info is None:
            raise ApiError(401, "your session has expired; log in again", headers=headers)
        if request.method not in SAFE_METHODS and not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), info.csrf_token
        ):
            raise ApiError(403, "missing or wrong X-CSRF-Token header")
        request.state.session = info
        return info.principal()
    raise ApiError(
        401, "send an API token as 'Authorization: Bearer <token>' or log in", headers=headers
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


def _page(items: list[dict[str, Any]], cursor: str | None, limit: int) -> Page[Any]:
    items = sorted(items, key=lambda o: (o["name"], o["id"]))
    if cursor:
        items = [o for o in items if (o["name"], o["id"]) > tuple(cursor.split("|", 1))]
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
        name: str | None = None,
        change_set: UUID | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Page[Any]:
        """List the tenant's objects of this type, by name; with ``change_set`` the draft view."""
        items = await _view(services, who, tenant, object_type, change_set)
        if name:
            items = [o for o in items if o["name"] == name]
        return _page(items, cursor, limit)

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
    app.openapi_schema = schema
    return schema


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


def create_app(services: ApiServices, *, public_url: str | None = None) -> FastAPI:
    """Build the application over ready-made services.

    Args:
        services: The wired services.
        public_url: External base URL (``https://switchyard.example.test``), used to build login
            redirect URIs and to decide whether cookies are ``Secure``.
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
        openapi_url="/api/v1/openapi.json",
    )
    app.state.services = services
    app.state.public_url = public_url
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

    router = APIRouter(prefix="/api/v1")

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
            "break_glass": sessions.break_glass_enabled,
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
        if error or not code or not state:
            raise ApiError(400, "the identity provider reported an error: " + (error or "no code"))
        try:
            result = await sessions.complete_login(
                state=state, code=code, redirect_uri=callback_url(request, provider)
            )
        except LoginError as exc:
            raise ApiError(401, str(exc)) from exc
        response = Response(status_code=302, headers={"Location": result.return_to})
        set_session_cookie(request, response, result.cookie, result.session)
        return response

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
            result = await sessions.break_glass_login(body.username, body.password, body.totp)
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
        return {
            "id": str(session.id) if session else None,
            "name": session.name if session else who.actor,
            "email": session.email if session else None,
            "is_system": who.is_system,
            "break_glass": bool(session and session.break_glass),
            "auth": "session" if session else "token",
            "csrf_token": session.csrf_token if session else None,
            "tenants": [
                {"id": str(t.id), "name": t.name, "role": who.role_in(t.id).value} for t in tenants
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
            oidc_group=body.oidc_group,
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
        return {
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
            request_id=_rid(),
        )
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
        response_model=Page[RevisionSummary],
        responses=_ok({"items": [ex.REVISION_SUMMARY], "next_cursor": None}),
    )
    async def revisions(
        tenant: Tenant,
        who: Who,
        svc: Services,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Page[Any]:
        """List applied revisions, newest first."""
        records = await svc.change_sets.revisions(
            who, tenant.id, limit=limit + 1, before=_int_cursor(cursor)
        )
        window = records[:limit]
        return Page(
            items=[
                RevisionSummary(
                    number=r.number,
                    author=r.author,
                    summary=r.summary,
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
        return [
            {"id": str(e.id), "name": e.name, "url": e.dataplane_url, "state": e.state.value}
            for e in await svc.engines.list_engines(who, tenant.id, group)
        ]

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
        return {"id": str(e.id), "name": e.name, "url": e.dataplane_url, "state": e.state.value}

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
        """Store an uploaded certificate and key, and add the certificate object to a change set."""
        if svc.certs is None:
            raise ApiError(503, "no secrets backend is configured for certificates")
        data = await svc.certs.import_pem(who, tenant.id, body.name, body.pem)
        op = ChangeOp(
            object_type=ObjectType.CERTIFICATE, object_id=uuid7(), action=Action.CREATE, body=data
        )
        return await _write(
            svc, who, tenant, op, change_set, apply, f"import certificate {body.name}"
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
            request_id=_rid(),
        )
        return {"job_id": str(job)}

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
        """Grow an SE group to ``count`` engines by cloning the SE template on Proxmox."""
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
        )
        job = await svc.infra.request_deploy(who, tenant.id, request, request_id=_rid())
        return {"job_id": str(job)}

    # live status
    def status_json(s: EngineStatus) -> dict[str, Any]:
        return {
            "engine": s.name,
            "engine_id": str(s.engine_id),
            "se_group_id": str(s.se_group_id),
            "reachable": s.reachable,
            "error": s.error,
            "haproxy_fingerprint": s.haproxy_fingerprint,
            "members": s.members,
            "agent": s.agent,
            "drift": s.drift,
            "observed_at": s.observed_at,
        }

    @router.get(
        "/tenants/{t}/status/engines",
        tags=["status"],
        summary="Last observed state of every engine",
        response_model=list[EngineStatusRead],
        responses=_ok([ex.ENGINE_STATUS]),
    )
    async def engine_status(tenant: Tenant, who: Who, svc: Services) -> list[dict[str, Any]]:
        return [status_json(s) for s in await svc.status.list_status(who, tenant.id)]

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
        return [
            {"id": v.id, "name": v.name, "protocol": v.protocol, "state": v.state}
            for v in await svc.status.virtual_services(who, tenant.id)
        ]

    @router.get(
        "/tenants/{t}/status/stream",
        tags=["status"],
        summary="Stream engine status (server-sent events)",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "`text/event-stream`; each `status` event carries the JSON array "
                "of `GET /status/engines`.",
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
        """Server-sent events: the engine status whenever it changes (and once at the start)."""
        import asyncio
        import json

        async def events() -> AsyncIterator[str]:
            last = ""
            while True:
                payload = json.dumps(
                    [
                        EngineStatusRead.model_validate(status_json(s)).model_dump(mode="json")
                        for s in await svc.status.list_status(who, tenant.id)
                    ],
                    sort_keys=True,
                )
                if payload != last:
                    yield f"event: status\ndata: {payload}\n\n"
                    last = payload
                else:
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
        kind: str | None = None,
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
        response_model=list[AuditEventRead],
        responses=_ok([ex.AUDIT_EVENT]),
    )
    async def audit_events(
        tenant: Tenant, who: Who, svc: Services, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from switchyard.db.ops_models import AuditEventRow

        async with tenant_session(svc.db.app, [tenant.id]) as session:
            rows = (
                await session.execute(
                    select(AuditEventRow).order_by(AuditEventRow.at.desc()).limit(limit)
                )
            ).scalars()
            return [
                {
                    "at": r.at,
                    "actor": r.actor,
                    "action": r.action,
                    "object": r.object_ref,
                    "diff": r.diff,
                    "request_id": r.request_id,
                }
                for r in rows
            ]

    app.include_router(router)
    app.openapi = lambda: _problem_json_openapi(app)  # type: ignore[method-assign]
    return app

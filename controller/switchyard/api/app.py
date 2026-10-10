"""The FastAPI application: ``/api/v1``.

Authentication is a bearer API token (see :mod:`switchyard.services.tokens`); OIDC/SAML login will
issue short-lived sessions in front of the same principal model. Errors are RFC 9457 problem+json.
Writes to desired state always go through a change set: ``?change_set=<id>`` adds an operation to a
draft, ``?apply=true`` opens an implicit single-operation change set and applies it.
"""

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, create_model

from switchyard.api.problems import ApiError, install_handlers
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import models as dm
from switchyard.domain.changes import (
    COLLECTION,
    Action,
    ChangeOp,
    DraftStatus,
    ObjectType,
)
from switchyard.domain.workflow import ChangeSetState, EngineState, Role
from switchyard.ids import uuid7
from switchyard.pipeline.engines import EngineDirectory
from switchyard.services.changesets import ChangeSetDetail, ChangeSetService
from switchyard.services.context import Databases, Principal
from switchyard.services.engines import EngineService
from switchyard.services.members import MemberService
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


def build_services(db: Databases, directory: EngineDirectory) -> ApiServices:
    """Wire the services over one set of database handles."""
    return ApiServices(
        db=db,
        tenants=TenantService(db),
        tokens=TokenService(db),
        change_sets=ChangeSetService(db),
        engines=EngineService(db),
        members=MemberService(db, directory),
    )


# -- response models ------------------------------------------------------------------------------


class Page(BaseModel):
    """A page of results."""

    items: list[dict[str, Any]]
    next_cursor: str | None = None


class Accepted(BaseModel):
    """Returned when a write was queued or recorded in a change set."""

    change_set_id: UUID
    object_id: UUID | None = None
    job_id: UUID | None = None
    op_index: int | None = None


class OpIn(BaseModel):
    """One operation in a change set."""

    model_config = ConfigDict(extra="forbid")
    object_type: ObjectType
    object_id: UUID
    action: Action
    body: dict[str, Any] | None = None
    expected_version: int | None = None


class OpsIn(BaseModel):
    """Operations to append to a change set."""

    ops: list[OpIn] = Field(min_length=1)


class OpenIn(BaseModel):
    """Open a draft."""

    summary: str = Field(default="", max_length=500)


class ReasonIn(BaseModel):
    """A free-text reason or comment."""

    comment: str = Field(min_length=1, max_length=1000)


class ApplyIn(BaseModel):
    """Options for apply."""

    break_glass_reason: str | None = Field(default=None, max_length=1000)


class MemberStateIn(BaseModel):
    """New runtime state for a pool member."""

    state: dm.AdminState


class TenantIn(BaseModel):
    """Create a tenant."""

    name: str
    oidc_group: str | None = None
    quotas: dict[str, int] = Field(default_factory=dict)
    require_approval: bool = False


class TokenIn(BaseModel):
    """Create an API token."""

    name: str
    role: Role
    ttl_days: int | None = Field(default=90, ge=1, le=3650)


class EngineIn(BaseModel):
    """Register a service engine."""

    name: str
    dataplane_url: str
    credentials_ref: str | None = None
    mgmt_ip: str | None = None
    agent_url: str | None = None


# -- dependencies ---------------------------------------------------------------------------------


def _services(request: Request) -> ApiServices:
    services: ApiServices = request.app.state.services
    return services


Services = Annotated[ApiServices, Depends(_services)]


async def current_principal(
    services: Services, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    """Authenticate the bearer token."""
    headers = {"WWW-Authenticate": "Bearer"}
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "send an API token as 'Authorization: Bearer <token>'", headers=headers)
    principal = await services.tokens.authenticate(authorization[7:].strip())
    if principal is None:
        raise ApiError(401, "invalid, expired or revoked token", headers=headers)
    return principal


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
_MODELS: dict[ObjectType, type[BaseModel] | None] = {
    ObjectType.NETWORK: dm.Network,
    ObjectType.VIP_POOL: dm.VipPool,
    ObjectType.SE_GROUP: dm.SeGroup,
    ObjectType.VIRTUAL_SERVICE: dm.VirtualService,
    ObjectType.POOL: dm.Pool,
    ObjectType.HEALTH_MONITOR: dm.HealthMonitor,
    ObjectType.CERTIFICATE: dm.Certificate,
    ObjectType.POLICY_SET: None,
    ObjectType.ROUTING_PROFILE: None,
}
_RESERVED = {"id", "tenant_id", "version"}


def _body_models(name: str, model: type[BaseModel]) -> tuple[type[BaseModel], type[BaseModel]]:
    """Create/patch request models: the domain fields without id, tenant and version."""
    create: dict[str, Any] = {}
    patch: dict[str, Any] = {}
    for key, field in model.model_fields.items():
        if key in _RESERVED:
            continue
        create[key] = (field.annotation, field)
        patch[key] = (field.annotation | None, None)  # type: ignore[operator]
    config = ConfigDict(extra="forbid")
    return (
        create_model(f"{name}Create", __config__=config, **create),
        create_model(f"{name}Patch", __config__=config, **patch),
    )


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


def _page(items: list[dict[str, Any]], cursor: str | None, limit: int) -> Page:
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
    model = _MODELS[object_type]
    base = f"/tenants/{{t}}/{slug}"
    tag = slug
    if model is None:  # policy sets: a union, so bodies stay free-form dicts validated on apply
        create_in: Any = dict[str, Any]
        patch_in: Any = dict[str, Any]
    else:
        create_in, patch_in = _body_models(model.__name__, model)

    @router.get(base, tags=[tag], name=f"list_{slug}")
    async def list_objects(
        tenant: Tenant,
        who: Who,
        services: Services,
        name: str | None = None,
        change_set: UUID | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Page:
        items = await _view(services, who, tenant, object_type, change_set)
        if name:
            items = [o for o in items if o["name"] == name]
        return _page(items, cursor, limit)

    @router.get(base + "/{object_id}", tags=[tag], name=f"get_{slug}")
    async def get_object(
        object_id: UUID,
        tenant: Tenant,
        who: Who,
        services: Services,
        response: Response,
        change_set: UUID | None = None,
    ) -> dict[str, Any]:
        for item in await _view(services, who, tenant, object_type, change_set):
            if item["id"] == str(object_id):
                response.headers["ETag"] = _etag(item["version"])
                return item
        raise ApiError(404, f"{slug} {object_id} not found")

    @router.post(base, tags=[tag], status_code=202, name=f"create_{slug}")
    async def create_object(
        body: create_in,
        tenant: Tenant,
        who: Who,
        services: Services,
        change_set: UUID | None = None,
        apply: bool = False,
        object_id: UUID | None = None,
    ) -> Accepted:
        data = body if isinstance(body, dict) else body.model_dump(mode="json", exclude_unset=True)
        op = ChangeOp(
            object_type=object_type, object_id=object_id or uuid7(), action=Action.CREATE, body=data
        )
        return await _write(
            services, who, tenant, op, change_set, apply, f"create {slug}/{data.get('name')}"
        )

    @router.patch(base + "/{object_id}", tags=[tag], status_code=202, name=f"update_{slug}")
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
        data = body if isinstance(body, dict) else body.model_dump(mode="json", exclude_unset=True)
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

    @router.delete(base + "/{object_id}", tags=[tag], status_code=202, name=f"delete_{slug}")
    async def delete_object(
        object_id: UUID,
        tenant: Tenant,
        who: Who,
        services: Services,
        change_set: UUID | None = None,
        apply: bool = False,
        if_match: Annotated[str | None, Header()] = None,
    ) -> Accepted:
        op = ChangeOp(
            object_type=object_type,
            object_id=object_id,
            action=Action.DELETE,
            expected_version=_if_match(if_match),
        )
        return await _write(
            services, who, tenant, op, change_set, apply, f"delete {slug}/{object_id}"
        )


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
        "created_at": r.created_at.isoformat(),
        "ops": [o.model_dump(mode="json") for o in detail.ops],
    }


def create_app(services: ApiServices) -> FastAPI:
    """Build the application over ready-made services."""
    app = FastAPI(
        title="Switchyard API",
        version="1.0.0",
        description="Multi-tenant load balancer controller.",
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
    )
    app.state.services = services
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

    @router.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # tenants
    @router.get("/tenants", tags=["tenants"])
    async def list_tenants(who: Who, svc: Services) -> list[dict[str, Any]]:
        return [t.__dict__ | {"id": str(t.id)} for t in await svc.tenants.list_tenants(who)]

    @router.post("/tenants", status_code=201, tags=["tenants"])
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

    @router.get("/tenants/{t}", tags=["tenants"])
    async def get_tenant(tenant: Tenant, response: Response) -> dict[str, Any]:
        response.headers["ETag"] = _etag(tenant.version)
        return tenant.__dict__ | {"id": str(tenant.id)}

    @router.patch("/tenants/{t}", tags=["tenants"])
    async def patch_tenant(
        body: dict[str, Any],
        tenant: Tenant,
        who: Who,
        svc: Services,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        t = await svc.tenants.update(
            who, tenant.id, expected_version=_if_match(if_match), changes=body, request_id=_rid()
        )
        return t.__dict__ | {"id": str(t.id)}

    @router.delete("/tenants/{t}", status_code=204, tags=["tenants"])
    async def delete_tenant(tenant: Tenant, who: Who, svc: Services) -> Response:
        await svc.tenants.delete(who, tenant.id, request_id=_rid())
        return Response(status_code=204)

    for slug, object_type in COLLECTIONS.items():
        _object_routes(router, slug, object_type)

    # change sets
    @router.get("/tenants/{t}/change-sets", tags=["change-sets"])
    async def list_change_sets(
        tenant: Tenant, who: Who, svc: Services, state: ChangeSetState | None = None
    ) -> list[dict[str, Any]]:
        records = await svc.change_sets.list_change_sets(
            who, tenant.id, states={state} if state else None
        )
        return [
            {"id": str(r.id), "state": r.state.value, "author": r.author, "summary": r.summary}
            for r in records
        ]

    @router.post("/tenants/{t}/change-sets", status_code=201, tags=["change-sets"])
    async def open_change_set(
        body: OpenIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        return _detail_json(
            await svc.change_sets.open(who, tenant.id, body.summary, request_id=_rid())
        )

    @router.get("/tenants/{t}/change-sets/{cs}", tags=["change-sets"])
    async def get_change_set(cs: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, Any]:
        return _detail_json(await svc.change_sets.get(who, tenant.id, cs))

    @router.post("/tenants/{t}/change-sets/{cs}/ops", tags=["change-sets"])
    async def add_ops(
        cs: UUID, body: OpsIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        ops = [ChangeOp(**o.model_dump()) for o in body.ops]
        return _detail_json(
            await svc.change_sets.add_ops(who, tenant.id, cs, ops, request_id=_rid())
        )

    @router.delete("/tenants/{t}/change-sets/{cs}/ops/{index}", tags=["change-sets"])
    async def remove_op(
        cs: UUID, index: int, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        return _detail_json(
            await svc.change_sets.remove_op(who, tenant.id, cs, index, request_id=_rid())
        )

    @router.get("/tenants/{t}/change-sets/{cs}/diff", tags=["change-sets"])
    async def diff(cs: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, Any]:
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

    @router.post("/tenants/{t}/change-sets/{cs}/apply", status_code=202, tags=["change-sets"])
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

    @router.post("/tenants/{t}/change-sets/{cs}/discard", tags=["change-sets"])
    async def discard(cs: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, str]:
        r = await svc.change_sets.discard(who, tenant.id, cs, request_id=_rid())
        return {"state": r.state.value}

    @router.post("/tenants/{t}/change-sets/{cs}/request-approval", tags=["change-sets"])
    async def request_approval(cs: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, str]:
        r = await svc.change_sets.request_approval(who, tenant.id, cs, request_id=_rid())
        return {"state": r.state.value}

    @router.post("/tenants/{t}/change-sets/{cs}/approve", status_code=202, tags=["change-sets"])
    async def approve(cs: UUID, tenant: Tenant, who: Who, svc: Services) -> dict[str, str]:
        return {"job_id": str(await svc.change_sets.approve(who, tenant.id, cs, request_id=_rid()))}

    @router.post("/tenants/{t}/change-sets/{cs}/reject", tags=["change-sets"])
    async def reject(
        cs: UUID, body: ReasonIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, str]:
        r = await svc.change_sets.reject(who, tenant.id, cs, body.comment, request_id=_rid())
        return {"state": r.state.value}

    # revisions
    @router.get("/tenants/{t}/revisions", tags=["revisions"])
    async def revisions(tenant: Tenant, who: Who, svc: Services) -> list[dict[str, Any]]:
        return [
            {
                "number": r.number,
                "author": r.author,
                "summary": r.summary,
                "git_sha": r.git_sha,
                "created_at": r.created_at.isoformat(),
            }
            for r in await svc.change_sets.revisions(who, tenant.id)
        ]

    @router.get("/tenants/{t}/revisions/{n}", tags=["revisions"])
    async def revision(n: int, tenant: Tenant, who: Who, svc: Services) -> dict[str, Any]:
        r = await svc.change_sets.revision(who, tenant.id, n)
        return {
            "number": r.number,
            "author": r.author,
            "summary": r.summary,
            "git_sha": r.git_sha,
            "snapshot": r.snapshot,
        }

    @router.post("/tenants/{t}/revisions/{n}/rollback", status_code=201, tags=["revisions"])
    async def rollback(n: int, tenant: Tenant, who: Who, svc: Services) -> dict[str, Any]:
        return _detail_json(await svc.change_sets.rollback(who, tenant.id, n, request_id=_rid()))

    # runtime
    @router.put("/tenants/{t}/pools/{pool}/members/{member}/state", tags=["pools"])
    async def member_state(
        pool: UUID, member: UUID, body: MemberStateIn, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, Any]:
        r = await svc.members.set_state(who, tenant.id, pool, member, body.state, request_id=_rid())
        return {"state": r.state.value, "applied": r.applied, "failed": r.failed}

    # engines
    @router.get("/tenants/{t}/se-groups/{group}/engines", tags=["engines"])
    async def list_engines(
        group: UUID, tenant: Tenant, who: Who, svc: Services
    ) -> list[dict[str, Any]]:
        return [
            {"id": str(e.id), "name": e.name, "url": e.dataplane_url, "state": e.state.value}
            for e in await svc.engines.list_engines(who, tenant.id, group)
        ]

    @router.post("/tenants/{t}/se-groups/{group}/engines", status_code=201, tags=["engines"])
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

    @router.put("/tenants/{t}/engines/{engine}/state", tags=["engines"])
    async def engine_state(
        engine: UUID, state: EngineState, tenant: Tenant, who: Who, svc: Services
    ) -> dict[str, str]:
        e = await svc.engines.set_state(who, tenant.id, engine, state, request_id=_rid())
        return {"state": e.state.value}

    @router.delete("/tenants/{t}/engines/{engine}", status_code=204, tags=["engines"])
    async def remove_engine(engine: UUID, tenant: Tenant, who: Who, svc: Services) -> Response:
        await svc.engines.remove(who, tenant.id, engine, request_id=_rid())
        return Response(status_code=204)

    # tokens
    @router.get("/tenants/{t}/tokens", tags=["tokens"])
    async def list_tokens(tenant: Tenant, who: Who, svc: Services) -> list[dict[str, Any]]:
        return [
            {
                "id": str(r.id),
                "name": r.name,
                "role": r.role.value,
                "revoked": r.revoked_at is not None,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            }
            for r in await svc.tokens.list_tokens(who, tenant.id)
        ]

    @router.post("/tenants/{t}/tokens", status_code=201, tags=["tokens"])
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

    @router.delete("/tenants/{t}/tokens/{token}", status_code=204, tags=["tokens"])
    async def revoke_token(token: UUID, tenant: Tenant, who: Who, svc: Services) -> Response:
        await svc.tokens.revoke(who, tenant.id, token, request_id=_rid())
        return Response(status_code=204)

    # jobs and audit
    @router.get("/jobs/{job_id}", tags=["jobs"])
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

    @router.get("/tenants/{t}/audit-events", tags=["audit"])
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
                    "at": r.at.isoformat(),
                    "actor": r.actor,
                    "action": r.action,
                    "object": r.object_ref,
                    "diff": r.diff,
                    "request_id": r.request_id,
                }
                for r in rows
            ]

    app.include_router(router)
    return app

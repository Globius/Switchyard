"""Typed request and response models of the HTTP API.

Every 2xx body has a declared model so the OpenAPI document (and the TypeScript client made from
it) never falls back to ``unknown``. The only free-form values left are the ones that are free-form
by nature: the partial ``body`` of a change operation, a job's ``result`` and an audit event's
``diff``.

Per object type there is a ``*Read`` model: the domain fields plus ``draft_status`` and
``changed_fields``. Policy sets and routing profiles are discriminated unions on ``kind``, with
``*Create``, ``*Patch`` and ``*Read`` variants per member (see ADR 0014).

Example:
    >>> from switchyard.domain import models as dm
    >>> read_model(dm.Pool).__name__
    'PoolRead'
"""

import operator
from datetime import datetime
from enum import StrEnum
from functools import reduce
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model

from switchyard.domain import models as dm
from switchyard.domain.changes import Action, DraftStatus, ObjectType
from switchyard.domain.snapshot import Snapshot
from switchyard.domain.workflow import ChangeSetState, EngineState, Role


class ApiModel(BaseModel):
    """Base of every response model: unknown fields are an error, so drift is caught by tests."""

    model_config = ConfigDict(extra="forbid")


class Page[T](ApiModel):
    """A page of results; pass ``next_cursor`` back as ``cursor`` to get the next page."""

    items: list[T]
    next_cursor: str | None = Field(
        default=None, description="Opaque cursor of the next page, or null on the last page."
    )


# -- problems -------------------------------------------------------------------------------------


class ProblemDetail(ApiModel):
    """One field-level finding inside a problem response (``errors[]``)."""

    path: str = Field(description="Dotted path of the offending input, for example `ports.0.port`.")
    code: str
    message: str


class ProblemOut(ApiModel):
    """An RFC 9457 ``application/problem+json`` error body."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    errors: list[ProblemDetail] | None = None


PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemOut, "description": "Missing, invalid or expired credentials."},
    403: {"model": ProblemOut, "description": "The caller's role does not allow this."},
    404: {"model": ProblemOut, "description": "The tenant or object does not exist."},
}
IF_MATCH_RESPONSES: dict[int | str, dict[str, Any]] = {
    412: {
        "model": ProblemOut,
        "description": "`If-Match` does not match the object's current version (stale write).",
    },
    428: {"model": ProblemOut, "description": "`If-Match` is required for this request."},
}
VALIDATION_RESPONSE: dict[int | str, dict[str, Any]] = {
    422: {
        "model": ProblemOut,
        "description": "The request or the resulting state breaks a rule; see `errors[].path`.",
    }
}
ETAG_HEADER: dict[str, Any] = {
    "ETag": {
        "description": 'The object\'s version as a quoted integer, for example `"3"`. '
        "Send it back as `If-Match` on PATCH and DELETE.",
        "schema": {"type": "string"},
    }
}

# -- generic ---------------------------------------------------------------------------------------


class Health(ApiModel):
    """Liveness answer."""

    status: Literal["ok"] = "ok"


class Accepted(ApiModel):
    """Returned when a write was queued or recorded in a change set."""

    change_set_id: UUID
    object_id: UUID | None = None
    job_id: UUID | None = None
    op_index: int | None = None


class JobAccepted(ApiModel):
    """A job was queued; poll ``GET /jobs/{job_id}``."""

    job_id: UUID


class StateOut(ApiModel):
    """The new state of a change set."""

    state: ChangeSetState


class EngineStateOut(ApiModel):
    """The new state of an engine."""

    state: EngineState


# -- object read models ----------------------------------------------------------------------------


def read_model(model: type[BaseModel]) -> type[BaseModel]:
    """The ``*Read`` model of a domain model: its fields plus the draft markers.

    Example:
        >>> from switchyard.domain import models as dm
        >>> sorted(read_model(dm.Network).model_fields)[:3]
        ['changed_fields', 'cidr', 'draft_status']
    """
    return create_model(
        f"{model.__name__}Read",
        __base__=model,
        __doc__=f"{model.__name__} as returned by the API.",
        draft_status=(
            DraftStatus,
            Field(description="Whether a draft (`?change_set=`) created, changed or deleted it."),
        ),
        changed_fields=(
            list[str],
            Field(description="Fields a draft changed; empty outside a draft."),
        ),
    )


_RESERVED = {"id", "tenant_id", "version"}


def body_models(model: type[BaseModel]) -> tuple[type[BaseModel], type[BaseModel]]:
    """Create and patch request models: the domain fields without id, tenant and version.

    A ``kind`` discriminator stays required in both so a union of models can be told apart.
    """
    create: dict[str, Any] = {}
    patch: dict[str, Any] = {}
    for key, field in model.model_fields.items():
        if key in _RESERVED:
            continue
        if key == "kind":
            create[key] = (field.annotation, Field(description="Which kind of object this is."))
            patch[key] = (field.annotation, Field(description="Must equal the object's kind."))
            continue
        create[key] = (field.annotation, field)
        patch[key] = (field.annotation | None, None)  # type: ignore[operator]
    config = ConfigDict(extra="forbid")
    return (
        create_model(f"{model.__name__}Create", __config__=config, **create),
        create_model(f"{model.__name__}Patch", __config__=config, **patch),
    )


def _union(members: list[Any]) -> Any:
    if len(members) == 1:
        return members[0]
    return Annotated[reduce(operator.or_, members), Field(discriminator="kind")]


class ObjectApi:
    """The request and response types of one object collection."""

    def __init__(self, members: tuple[type[BaseModel], ...]) -> None:
        """Build create, patch and read variants for each member and union them on ``kind``."""
        bodies = [body_models(m) for m in members]
        reads = [read_model(m) for m in members]
        self.create: Any = _union([c for c, _ in bodies])
        self.patch: Any = _union([p for _, p in bodies])
        self.read: Any = _union(reads)
        self.read_members = reads


#: Domain model(s) of each object type; more than one means a union discriminated by ``kind``.
OBJECT_MODELS: dict[ObjectType, tuple[type[BaseModel], ...]] = {
    ObjectType.NETWORK: (dm.Network,),
    ObjectType.VIP_POOL: (dm.VipPool,),
    ObjectType.SE_GROUP: (dm.SeGroup,),
    ObjectType.VIRTUAL_SERVICE: (dm.VirtualService,),
    ObjectType.POOL: (dm.Pool,),
    ObjectType.HEALTH_MONITOR: (dm.HealthMonitor,),
    ObjectType.CERTIFICATE: (dm.Certificate,),
    ObjectType.POLICY_SET: (dm.RedirectPolicySet, dm.AllowListPolicySet),
    ObjectType.ROUTING_PROFILE: (dm.VrrpProfile, dm.BgpProfile),
}
OBJECT_APIS: dict[ObjectType, ObjectApi] = {t: ObjectApi(m) for t, m in OBJECT_MODELS.items()}

# -- tenants, tokens, engines ----------------------------------------------------------------------


class TenantRead(ApiModel):
    """A tenant."""

    id: UUID
    name: str
    version: int
    oidc_group: str | None = None
    quotas: dict[str, int] = Field(description="Object-count limits by object type.")
    require_approval: bool


class TokenRead(ApiModel):
    """An API token (never the secret)."""

    id: UUID
    name: str
    role: Role
    revoked: bool
    expires_at: datetime | None = None


class TokenCreated(ApiModel):
    """A new API token; the plaintext is shown once."""

    id: UUID
    token: str
    role: Role


class EngineRead(ApiModel):
    """A service engine (SE VM) with where it runs and whether its agent has enrolled.

    Live health (reachable, drift, BGP/VRRP) is in `GET /status/engines`, not here.
    """

    id: UUID
    name: str
    url: str = Field(description="Data Plane API URL.")
    state: EngineState
    se_group_id: UUID
    mgmt_ip: str | None = Field(default=None, description="Management address (BGP router id).")
    agent_url: str | None = Field(default=None, description="`host:port` of `sy-agent`, if any.")
    pve_node: str | None = Field(
        default=None, description="Proxmox node of the VM; null for a hand-built engine."
    )
    vmid: int | None = Field(
        default=None, description="Proxmox VM id; null for a hand-built engine."
    )
    agent_enrolled: bool = Field(
        description="True once `sy-agent enroll` has traded a token for a client certificate."
    )
    agent_cert_serial: str | None = Field(
        default=None, description="Serial of the agent's client certificate."
    )


class EnrollmentToken(ApiModel):
    """A one-time token for ``sy-agent enroll`` (shown once)."""

    token: str
    expires_at: datetime


class EnrollOut(ApiModel):
    """The agent's certificate and renewal credentials."""

    engine: str
    certificate: str
    ca: str
    bundle_public_key: str
    renewal_token: str
    not_after: datetime


class MemberStateResult(ApiModel):
    """Result of changing a pool member's runtime state on every engine of the group."""

    state: dm.AdminState
    applied: list[str] = Field(description="Engines that took the change.")
    failed: dict[str, str] = Field(description="Engine name to the reason it did not.")


# -- login -----------------------------------------------------------------------------------------


class AuthProvider(ApiModel):
    """A way to log in."""

    id: str
    name: str
    protocol: Literal["oidc", "saml"]


class AuthProviders(ApiModel):
    """Identity providers users can log in with."""

    providers: list[AuthProvider]
    break_glass: bool = Field(description="Whether the local break-glass login is enabled.")


class MeTenant(ApiModel):
    """A tenant the caller can act in and the role held there."""

    id: UUID
    name: str
    role: Role


class Me(ApiModel):
    """The authenticated caller."""

    id: str | None = Field(description="Session id; null for API tokens.")
    name: str
    email: str | None = None
    is_system: bool
    break_glass: bool
    auth: Literal["session", "token"]
    csrf_token: str | None = Field(
        description="Send as `X-CSRF-Token` on writes made with the session cookie."
    )
    tenants: list[MeTenant]


# -- change sets -----------------------------------------------------------------------------------


class ChangeOpRead(ApiModel):
    """One operation of a change set."""

    object_type: ObjectType
    object_id: UUID
    action: Action
    body: dict[str, Any] | None = Field(
        default=None, description="Create: the whole object. Update: the changed fields only."
    )
    expected_version: int | None = None


class ChangeSetSummary(ApiModel):
    """A change set in a list."""

    id: UUID
    state: ChangeSetState
    author: str
    summary: str
    base_revision: int
    created_at: datetime
    op_count: int


class ChangeSetRead(ApiModel):
    """A change set with its operations."""

    id: UUID
    state: ChangeSetState
    author: str
    summary: str
    base_revision: int
    approver: str | None = None
    reject_comment: str | None = None
    error: str | None = None
    created_at: datetime
    ops: list[ChangeOpRead]


class DiffObject(ApiModel):
    """An object a change set creates, changes or deletes."""

    type: ObjectType
    id: UUID
    name: str
    status: DraftStatus
    changed_fields: list[str]


class DiffProblem(ApiModel):
    """A rule the proposed state breaks; blocks apply."""

    path: str
    code: str
    message: str


class DiffWarning(ApiModel):
    """Something worth knowing about the proposed state; never blocks apply."""

    code: str = Field(description="Stable identifier of the warning.")
    path: str
    message: str


class DiffFile(ApiModel):
    """One rendered file that changes."""

    path: str
    status: Literal["added", "removed", "modified"]
    diff: str = Field(description="Unified diff, a/<path> to b/<path>.")
    before: str | None = Field(description="Previous content; null when the file is added.")
    after: str | None = Field(description="New content; null when the file is removed.")


class DiffGroup(ApiModel):
    """The rendered difference for one SE group."""

    id: UUID
    name: str
    error: str | None = Field(description="Why rendering failed, if it did.")
    files: list[DiffFile]


class ChangeSetDiff(ApiModel):
    """What applying a change set would change."""

    objects: list[DiffObject]
    problems: list[DiffProblem]
    warnings: list[DiffWarning]
    se_groups: list[DiffGroup]


# -- revisions -------------------------------------------------------------------------------------


class RevisionSummary(ApiModel):
    """A revision in a list."""

    number: int
    author: str
    summary: str
    git_sha: str | None = None
    created_at: datetime


class RevisionRead(RevisionSummary):
    """A revision with the full desired state it recorded."""

    snapshot: Snapshot


class ProtocolRulesRead(ApiModel):
    """The fields a protocol allows, so a form can disable the rest (ticket T7)."""

    protocol: dm.Protocol
    service_forbidden: list[str] = Field(description="Virtual service fields that are rejected.")
    port_forbidden: list[str] = Field(description="Port fields that are rejected.")
    pool_forbidden: list[str] = Field(description="Pool fields rejected on the service's pools.")
    algorithms: list[dm.Algorithm]
    monitor_types: list[dm.MonitorType]
    member_port_required: bool


# -- status ----------------------------------------------------------------------------------------


class MemberStatus(ApiModel):
    """Health of one pool member as HAProxy sees it."""

    backend: str
    server: str
    status: str = Field(description="HAProxy status, for example `UP`, `DOWN`, `MAINT`.")


class BgpSessionStatus(ApiModel):
    """One BGP neighbour."""

    peer: str
    state: str
    remote_asn: int


class VrrpInstanceStatus(ApiModel):
    """One VRRP instance."""

    name: str
    state: str


class IpvsRealServerStatus(ApiModel):
    """One IPVS real server of a UDP virtual service, with its counters."""

    service: str = Field(description="Virtual service name.")
    vip: str
    port: int
    address: str
    member_port: int
    state: str = Field(description="`up`, `down` or `drain`.")
    weight: int
    active_conns: int
    inactive_conns: int
    in_packets: int
    out_packets: int
    in_bytes: int
    out_bytes: int


class AgentStatusRead(ApiModel):
    """What the engine's agent reports."""

    version: str
    bundle_id: str
    haproxy_healthy: bool
    routes_withdrawn: bool
    bgp: list[BgpSessionStatus]
    vrrp: list[VrrpInstanceStatus]
    ipvs: list[IpvsRealServerStatus] = Field(
        default_factory=list, description="UDP real servers; empty without UDP services."
    )
    file_hashes: dict[str, str]


class VirtualServiceState(StrEnum):
    """Whether the data plane renders a virtual service."""

    SERVING = "serving"
    DISABLED = "disabled"
    NOT_SERVED = "not_served"


class VirtualServiceHealth(StrEnum):
    """Health of a virtual service worked out from its pool members' check results."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


class VirtualServiceStatusRead(ApiModel):
    """State and health of one virtual service.

    `state` says whether the data plane renders it (`not_served` is reserved for one no engine
    can render). `health` says whether it can serve: look at the reported member rows of its
    pools (default and route pools) on the reachable engines of its SE group. `disabled`: the
    service is disabled. `unknown`: no engine of the group is reachable or none has reported
    yet. `healthy`: every row is `UP` and every reachable engine reported. `down`: no row is
    `UP`, or a pool has no members. `degraded`: some rows are `UP`, or a reachable engine did not
    report. Draining members are not `UP`.
    """

    id: UUID
    name: str
    protocol: str
    state: VirtualServiceState
    health: VirtualServiceHealth
    members_up: int = Field(description="Reported member rows that are `UP`, across engines.")
    members_total: int = Field(description="Reported member rows of the service's pools.")


class DriftKind(StrEnum):
    """What differs between desired and observed state."""

    HAPROXY_CONFIG = "haproxy_config"
    AGENT_BUNDLE = "agent_bundle"
    AGENT_FILE = "agent_file"
    AGENT_UNEXPECTED_FILE = "agent_unexpected_file"


class DriftItem(ApiModel):
    """One difference. Drift is reported, never corrected automatically."""

    kind: DriftKind
    expected: str | None = None
    actual: str | None = None
    path: str | None = None


class EngineStatusRead(ApiModel):
    """The last observed state of one engine."""

    engine: str
    engine_id: UUID
    se_group_id: UUID
    reachable: bool
    error: str | None = None
    haproxy_fingerprint: str | None = None
    members: list[MemberStatus]
    agent: AgentStatusRead | None = None
    drift: list[DriftItem]
    observed_at: datetime


# -- IPAM ------------------------------------------------------------------------------------------


class VipAllocation(ApiModel):
    """One address of a VIP pool that a virtual service holds."""

    address: str
    virtual_service_id: UUID
    virtual_service_name: str
    enabled: bool
    draft_status: DraftStatus = Field(
        description="`unchanged` for live reads; with `?change_set=` the draft's treatment."
    )


class VipPoolUsage(ApiModel):
    """How much of a VIP pool is in use. Addresses are held by the services that carry them."""

    pool_id: UUID
    total: int = Field(description="Usable addresses in the pool's ranges.")
    used: int
    free: int
    allocations: list[VipAllocation]
    next_free: str | None = Field(
        description="The address a new service would get (lowest free), or null when exhausted."
    )


class NextFree(ApiModel):
    """A suggested free address; nothing is reserved."""

    address: str


# -- jobs and audit --------------------------------------------------------------------------------


class JobRead(ApiModel):
    """A queued or finished background job."""

    id: UUID
    kind: str
    state: str
    attempts: int
    error: str | None = None
    result: dict[str, Any] | None = Field(default=None, description="Shape depends on `kind`.")


class AuditActionRead(ApiModel):
    """One kind of audit event."""

    action: str = Field(description="The dotted name found in `AuditEventRead.action`.")
    category: str = Field(description="The part of `action` before the first dot.")
    description: str


class AuditEventRead(ApiModel):
    """One audit-log entry."""

    at: datetime
    tenant_id: UUID | None = Field(
        default=None, description="The tenant concerned; null for system-level events."
    )
    actor: str
    action: str
    object: str | None = None
    diff: dict[str, Any] | None = None
    request_id: str | None = None


# -- request bodies --------------------------------------------------------------------------------


class BreakGlassIn(ApiModel):
    """Body of ``POST /auth/break-glass``."""

    username: str = Field(max_length=100)
    password: str = Field(max_length=200)
    totp: str = Field(min_length=6, max_length=8)


class EnrollIn(ApiModel):
    """Body of ``POST /enroll`` (no bearer token: the enrollment token is the credential)."""

    token: str = Field(max_length=200)
    csr: str = Field(max_length=16384, description="PEM certificate request made by the agent.")


class CertImportIn(ApiModel):
    """Body of ``POST /certificates/import``."""

    name: str
    pem: str = Field(max_length=65536, description="Certificate chain followed by the private key.")


class CertIssueIn(ApiModel):
    """Body of ``POST /certificates/issue``."""

    name: str
    source: Literal["acme", "cloudflare_origin"]
    sans: list[str] = Field(min_length=1, max_length=50)


class DeployIn(ApiModel):
    """Body of ``POST /se-groups/{id}/deploy``."""

    count: int = Field(ge=1, le=16, description="Total engines the group should have afterwards.")
    nodes: list[str] = Field(min_length=1, description="Proxmox nodes to spread new VMs over.")
    template_vmid: int = Field(ge=100)
    mgmt_pool_id: UUID
    pool: str | None = Field(default=None, max_length=64)
    credentials_ref: str | None = None
    ssh_public_keys: str | None = Field(default=None, max_length=8192)


class OpIn(ApiModel):
    """One operation in a change set."""

    object_type: ObjectType
    object_id: UUID
    action: Action
    body: dict[str, Any] | None = None
    expected_version: int | None = None


class OpsIn(ApiModel):
    """Operations to append to a change set."""

    ops: list[OpIn] = Field(min_length=1)


class OpenIn(ApiModel):
    """Open a draft."""

    summary: str = Field(default="", max_length=500)


class ReasonIn(ApiModel):
    """A free-text reason or comment."""

    comment: str = Field(min_length=1, max_length=1000)


class ApplyIn(ApiModel):
    """Options for apply."""

    break_glass_reason: str | None = Field(default=None, max_length=1000)


class MemberStateIn(ApiModel):
    """New runtime state for a pool member."""

    state: dm.AdminState


class TenantIn(ApiModel):
    """Create a tenant."""

    name: str
    oidc_group: str | None = None
    quotas: dict[str, int] = Field(default_factory=dict)
    require_approval: bool = False


class TenantPatch(ApiModel):
    """Change a tenant; send only the fields to change."""

    oidc_group: str | None = None
    quotas: dict[str, int] | None = None
    require_approval: bool | None = None


class TokenIn(ApiModel):
    """Create an API token."""

    name: str
    role: Role
    ttl_days: int | None = Field(default=90, ge=1, le=3650)


class EngineIn(ApiModel):
    """Register a service engine."""

    name: str
    dataplane_url: str
    credentials_ref: str | None = None
    mgmt_ip: str | None = None
    agent_url: str | None = None

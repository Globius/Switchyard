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

from pydantic import BaseModel, ConfigDict, Field, SecretStr, create_model, model_validator

from switchyard.domain import models as dm
from switchyard.domain.changes import Action, DraftStatus, ObjectType
from switchyard.domain.snapshot import Snapshot
from switchyard.domain.telemetry import AccessLogEntry, MetricSummary, ServiceMetrics
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


ProblemCode = Literal[
    "unauthenticated",
    "invalid_token",
    "session_expired",
    "csrf",
    "forbidden",
    "role_required",
    "user_disabled",
]
"""401/403 ``code`` values: no credentials, bad bearer token, session ended (401, sign in again),
missing or wrong CSRF header, no access to this tenant, role too weak (also sends
``required_role``), the person was disabled by an administrator."""


class ProblemDetail(ApiModel):
    """One field-level finding inside a problem response (``errors[]``)."""

    path: str = Field(description="Dotted path of the offending input, for example `ports.0.port`.")
    code: str
    message: str


class StaleConflict(ApiModel):
    """Extension member of a `412` caused by an operation's `expected_version`.

    To resolve: `GET` the object (its current state is "theirs", the `ETag` is
    `current_version`), compare with what the client had read (the base) and the operation's
    `body` ("yours"), then resend the operation with `expected_version: current_version`.
    """

    object_type: ObjectType
    object_id: UUID
    expected_version: int
    current_version: int


class ProblemOut(ApiModel):
    """An RFC 9457 ``application/problem+json`` error body."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: ProblemCode | None = Field(
        default=None,
        description="Stable identifier clients can switch on; always set on 401 and 403.",
    )
    required_role: Role | None = Field(
        default=None, description="With `code: role_required`: the weakest role that may do this."
    )
    errors: list[ProblemDetail] | None = None
    conflict: StaleConflict | None = Field(
        default=None, description="Present on a stale-version `412` from a change-set operation."
    )


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
    quotas: dict[str, int] = Field(
        description="Object-count limits by object type name (`virtual_service`, `pool`, ...)."
    )
    require_approval: bool
    usage: dict[str, int] = Field(
        default_factory=dict,
        description="Objects the tenant owns now, by object type name (zeros included).",
    )


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


class GrantSourceRead(ApiModel):
    """The provider group that granted a role."""

    provider: str
    group: str


class MeTenant(ApiModel):
    """A tenant the caller can act in and the role held there."""

    id: UUID
    name: str
    role: Role
    via: list[GrantSourceRead] = Field(
        description="The provider groups that granted this role; empty for break-glass and tokens."
    )


class MeSession(ApiModel):
    """The lifetime of the caller's cookie session."""

    expires_at: datetime = Field(
        description="Absolute end of the session; refresh does not move it."
    )
    idle_timeout_seconds: int = Field(
        description="The session ends after this long without a call."
    )
    grants_computed_at: datetime = Field(
        description="When roles were last recomputed from the provider groups and the mappings."
    )


class Me(ApiModel):
    """The authenticated caller."""

    id: str | None = Field(description="Session id; null for API tokens.")
    principal: str = Field(
        description="Author string the API stores for this caller's writes (a draft's `author`), "
        "such as `user:local:breakglass`; generally `user:<provider>:<subject>`."
    )
    name: str
    email: str | None = None
    is_system: bool
    break_glass: bool
    auth: Literal["session", "token"]
    csrf_token: str | None = Field(
        description="Send as `X-CSRF-Token` on writes made with the session cookie."
    )
    session: MeSession | None = Field(description="Null for API tokens.")
    tenants: list[MeTenant]


PermissionVerb = Literal[
    "apply",
    "apply_bypass",
    "approve",
    "cancel",
    "change",
    "create",
    "deploy",
    "disable",
    "edit",
    "enable",
    "list",
    "manage",
    "read",
    "reject",
    "request_approval",
    "retry",
    "rollback",
    "set_state",
    "write",
]
PermissionObject = Literal[
    "application",
    "break_glass",
    "certificate",
    "change_set",
    "draft",
    "engine",
    "enrollment_token",
    "identity_provider",
    "infrastructure",
    "job",
    "job_own",
    "member",
    "quota",
    "require_approval",
    "revision",
    "role_mapping",
    "role_mapping_privileged",
    "sdn",
    "se_group",
    "system_token",
    "tenant",
    "tenant_data",
    "tenant_user",
    "token",
    "user",
]


class PermissionRead(ApiModel):
    """One cell of the permission matrix. `verb` and `object` are published as enums."""

    verb: PermissionVerb
    object: PermissionObject
    min_role: Role = Field(description="The weakest role that may do this.")
    description: str = Field(description="One sentence for tooltips.")


class PermissionMatrix(ApiModel):
    """The permission matrix; `can(verb, object)` is `role_in(tenant) >= min_role`."""

    roles: list[Role] = Field(description="Roles from weakest to strongest.")
    permissions: list[PermissionRead]


class RoleMappingRead(ApiModel):
    """A provider group that grants a role."""

    id: UUID
    provider_id: UUID
    provider: str
    group_name: str
    label: str | None = Field(description="Free text for opaque group ids.")
    tenant_id: UUID | None = Field(description="Null only for `system-admin`.")
    tenant: str | None
    role: Role
    version: int
    created_by: str
    created_at: datetime


class RoleMappingIn(ApiModel):
    """Create a mapping on a tenant route (`/tenants/{t}/role-mappings`)."""

    provider: str = Field(description="Provider id or name.")
    group_name: str
    label: str | None = None
    role: Role


class SystemRoleMappingIn(RoleMappingIn):
    """Create a mapping on the system-wide route."""

    tenant_id: UUID | None = Field(
        default=None, description="The tenant; omit for `system-admin`, required otherwise."
    )


class RoleMappingPatch(ApiModel):
    """Change a mapping; send only the fields to change. The tenant of a mapping is fixed."""

    group_name: str | None = None
    label: str | None = None
    role: Role | None = None


class EffectiveGrantRead(ApiModel):
    """What a person can do in one tenant now."""

    tenant_id: UUID | None = Field(description="Null for `system-admin`.")
    tenant: str | None
    role: Role
    via: list[GrantSourceRead]


class UserRead(ApiModel):
    """A person who has signed in."""

    id: UUID
    provider_id: UUID
    provider: str
    username: str
    email: str | None
    groups: list[str] = Field(
        description="Groups from the last sign-in; empty on the tenant route."
    )
    first_login_at: datetime
    last_login_at: datetime
    last_seen_at: datetime
    disabled_at: datetime | None
    grants: list[EffectiveGrantRead] = Field(
        description="Effective grants now; on the tenant route only that tenant's."
    )


class UserDetail(UserRead):
    """A user with the number of live sessions."""

    subject: str
    active_sessions: int


class SessionsRevoked(ApiModel):
    """How many sessions `revoke-sessions` ended."""

    revoked: int


class _ProviderRead(ApiModel):
    """Fields every identity provider shows. Secrets are never returned."""

    id: UUID
    name: str
    enabled: bool
    version: int
    created_at: datetime
    warnings: list[str] = Field(
        description="Set on create and change only: findings that did not block it, "
        "for example an unreachable OIDC discovery document."
    )


class OidcProviderRead(_ProviderRead):
    """An OpenID Connect provider."""

    protocol: Literal["oidc"] = "oidc"
    issuer_url: str
    client_id: str
    client_secret_ref: str | None = Field(description="Reference into the secrets backend.")
    client_secret_set: bool = Field(description="Whether a client secret is stored (never shown).")
    scopes: str
    username_claim: str
    email_claim: str
    groups_claim: str


class SamlProviderRead(_ProviderRead):
    """A SAML 2.0 provider."""

    protocol: Literal["saml"] = "saml"
    idp_metadata_url: str | None
    idp_entity_id: str
    sso_url: str
    signing_certificates: list[str] = Field(description="Public PEM certificates.")
    sp_entity_id: str = Field(description="Our entity id (the stored one, or the default).")
    name_id_format: str | None
    sign_requests: bool
    sp_signing_key_ref: str | None = Field(description="Reference to the request signing key.")
    sp_signing_certificate: str | None = Field(
        description="Public certificate that verifies signed requests (also in the SP metadata)."
    )
    attribute_username: str
    attribute_email: str
    attribute_groups: str
    acs_url: str = Field(description="Assertion consumer service URL to give the IdP.")
    sp_metadata_url: str = Field(description="Public SP metadata URL to give the IdP.")


IdentityProviderRead = Annotated[
    OidcProviderRead | SamlProviderRead, Field(discriminator="protocol")
]


class OidcProviderIn(ApiModel):
    """Create an OpenID Connect provider. The client secret is write-only."""

    protocol: Literal["oidc"]
    name: str
    enabled: bool = True
    issuer_url: str
    client_id: str
    client_secret: str | None = Field(
        default=None, description="Stored in the secrets backend; never returned."
    )
    client_secret_ref: str | None = Field(
        default=None, description="Use an existing secret instead of sending one."
    )
    scopes: str | None = None
    username_claim: str | None = None
    email_claim: str | None = None
    groups_claim: str | None = None


class SamlProviderIn(ApiModel):
    """Create a SAML provider. Send metadata (URL or XML) or the fields it would fill in."""

    protocol: Literal["saml"]
    name: str
    enabled: bool = True
    idp_metadata_url: str | None = Field(default=None, description="https URL of the IdP metadata.")
    idp_metadata_xml: str | None = Field(default=None, description="The IdP metadata document.")
    idp_entity_id: str | None = None
    sso_url: str | None = None
    signing_certificates: list[str] | None = Field(default=None, description="Public PEM only.")
    sp_entity_id: str | None = None
    name_id_format: str | None = None
    sign_requests: bool | None = Field(
        default=None,
        description="Sign AuthnRequests. The controller generates the key (kept in the secrets "
        "backend) unless `sp_signing_key_ref` is given.",
    )
    sp_signing_key_ref: str | None = None
    attribute_username: str | None = None
    attribute_email: str | None = None
    attribute_groups: str | None = None


IdentityProviderIn = Annotated[OidcProviderIn | SamlProviderIn, Field(discriminator="protocol")]


class OidcProviderPatch(ApiModel):
    """Change an OIDC provider; send `protocol` and only the fields to change."""

    protocol: Literal["oidc"]
    name: str | None = None
    enabled: bool | None = None
    issuer_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    client_secret_ref: str | None = None
    scopes: str | None = None
    username_claim: str | None = None
    email_claim: str | None = None
    groups_claim: str | None = None


class SamlProviderPatch(ApiModel):
    """Change a SAML provider; send `protocol` and only the fields to change."""

    protocol: Literal["saml"]
    name: str | None = None
    enabled: bool | None = None
    idp_metadata_url: str | None = None
    idp_metadata_xml: str | None = None
    idp_entity_id: str | None = None
    sso_url: str | None = None
    signing_certificates: list[str] | None = None
    sp_entity_id: str | None = None
    name_id_format: str | None = None
    sign_requests: bool | None = None
    sp_signing_key_ref: str | None = None
    attribute_username: str | None = None
    attribute_email: str | None = None
    attribute_groups: str | None = None


IdentityProviderPatch = Annotated[
    OidcProviderPatch | SamlProviderPatch, Field(discriminator="protocol")
]


class IdpTestIn(ApiModel):
    """Start a test sign-in."""

    return_to: str | None = Field(
        default=None, description="Same-site path the browser returns to afterwards."
    )


class IdpTestStarted(ApiModel):
    """A test sign-in: open `authorize_url` in the browser, then poll `test_id`."""

    test_id: UUID
    authorize_url: str


class WouldGrant(ApiModel):
    """A grant the current mappings would give this person."""

    tenant: str | None = Field(description="Null for `system-admin`.")
    role: Role
    via: GrantSourceRead


class IdpTestResult(ApiModel):
    """The state of a test sign-in. Read once, within five minutes; then 404."""

    status: Literal["pending", "done"]
    subject: str | None = None
    username: str | None = None
    email: str | None = None
    groups: list[str] | None = None
    claims: dict[str, Any] | None = Field(
        default=None, description="Decoded claims without tokens, signatures or secrets."
    )
    would_grant: list[WouldGrant] | None = None
    warnings: list[str] | None = None
    error: str | None = Field(default=None, description="Why the provider refused, if it did.")


class BreakGlassAdminRead(ApiModel):
    """The enrolled break-glass administrator (never its password or TOTP secret)."""

    username: str
    totp_confirmed: bool
    created_at: datetime
    last_login_at: datetime | None
    locked_until: datetime | None


class BreakGlassRead(ApiModel):
    """Whether break-glass login is on, and the administrator."""

    forced_by_deployment: bool = Field(
        description="`SWITCHYARD_BREAK_GLASS=1`: on regardless of this API; read-only."
    )
    enabled: bool
    enabled_until: datetime | None
    enabled_by: str | None
    reason: str | None
    admin: BreakGlassAdminRead | None


class BreakGlassSwitchIn(ApiModel):
    """Switch break-glass login on (with a reason) or off."""

    enabled: bool
    ttl_minutes: int | None = Field(
        default=None, description="How long it stays on: 1 to 1440, default 240."
    )
    reason: str | None = Field(default=None, description="Required when enabling.")


class BreakGlassEnrollIn(ApiModel):
    """Enrol (or replace) the break-glass administrator."""

    username: str = "breakglass"


class BreakGlassEnrollment(ApiModel):
    """Shown once: the generated password and TOTP secret."""

    username: str
    password: str
    totp_secret: str
    otpauth_uri: str = Field(description="Render as a QR code for the authenticator app.")


class BreakGlassConfirmIn(ApiModel):
    """A code from the authenticator, proving enrolment worked."""

    totp: str


class SystemTokenRead(ApiModel):
    """A system API token (never the secret)."""

    id: UUID
    name: str
    role: Role
    revoked: bool
    expires_at: datetime | None
    created_by: str
    created_at: datetime
    last_used_at: datetime | None


class SystemTokenIn(ApiModel):
    """Create a system token."""

    name: str
    ttl_days: int = Field(default=30, ge=1, le=90, description="30 by default, 90 at most.")


class SystemTokenCreated(ApiModel):
    """A new system token; the plaintext is shown once."""

    id: UUID
    token: str
    role: Role
    expires_at: datetime | None


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


class ImpactService(ApiModel):
    """A virtual service a change reaches."""

    id: UUID
    name: str
    vip: str
    se_group_id: UUID


class ImpactGroup(ApiModel):
    """An SE group whose engines would receive new configuration."""

    id: UUID
    name: str


class DiffImpact(ApiModel):
    """What a change set reaches, for the review's Impact tab.

    A service is listed when it is created, changed or deleted, or uses a changed pool, health
    monitor, certificate, policy set, SE group, network, VIP pool or routing profile.
    """

    services: list[ImpactService]
    vips: list[str] = Field(description="VIP addresses of those services, ascending.")
    se_groups: list[ImpactGroup]
    hostnames: list[str] = Field(description="Route hosts of those services, sorted.")


class ChangeSetDiff(ApiModel):
    """What applying a change set would change."""

    objects: list[DiffObject]
    problems: list[DiffProblem]
    warnings: list[DiffWarning]
    impact: DiffImpact
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
    state: str = Field(
        description="FRR session state: `Idle`, `Connect`, `Active`, `OpenSent`, `OpenConfirm` "
        "or `Established` (only `Established` is healthy)."
    )
    remote_asn: int


class VrrpInstanceStatus(ApiModel):
    """One VRRP instance."""

    name: str
    state: str = Field(description="`MASTER`, `BACKUP` or `FAULT`.")


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
    routes_withdrawn: bool = Field(description="True while any BGP prefix is withdrawn.")
    withdrawn_prefixes: list[str] = Field(
        default_factory=list, description="The BGP prefixes the watchdog has withdrawn, sorted."
    )
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


class VirtualServiceMemberRead(ApiModel):
    """One pool member of a virtual service as one engine reports it."""

    pool: str
    pool_id: UUID
    member_id: UUID
    address: str
    port: int | None = None
    engine: str
    engine_id: UUID
    status: str = Field(description="HAProxy status, for example `UP`, `DOWN`, `DRAIN`.")
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
    members: list[VirtualServiceMemberRead] = Field(
        default_factory=list,
        description="The reported rows behind the counts, one per engine and pool member.",
    )


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


class MonitorTestIn(ApiModel):
    """Body of `POST /health-monitors/{id}/test`: which member to probe, and optional overrides."""

    pool_id: UUID
    member_id: UUID
    engine_id: UUID | None = Field(
        default=None,
        description="Run from this engine; default the first enabled one with an agent.",
    )
    port: int | None = Field(default=None, ge=1, le=65535, description="Override the check port.")
    http_path: str | None = Field(default=None, max_length=1024, description="Override the path.")
    expect_status: list[int] | None = Field(default=None, description="Override accepted codes.")
    timeout_s: int | None = Field(default=None, ge=1, le=10)


class MonitorTestOut(ApiModel):
    """Result of one monitor test. Nothing is saved."""

    ok: bool
    latency_ms: int
    detail: str = Field(description="What came back (for example `HTTP 200`) or why it failed.")
    engine: str = Field(description="The engine that ran the probe.")
    target: str = Field(description="`address:port` that was probed.")
    protocol: str


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


# -- telemetry -------------------------------------------------------------------------------------


class MetricsRead(ApiModel):
    """Metrics of one virtual service, or why there are none."""

    available: bool = Field(
        description="False when no metrics backend is configured or it cannot be reached."
    )
    reason: str | None = Field(default=None, description="Why `available` is false.")
    metrics: ServiceMetrics | None = None


class MetricsOverviewItem(ApiModel):
    """Headline numbers of one virtual service."""

    virtual_service_id: UUID
    name: str
    summary: MetricSummary


class MetricsOverview(ApiModel):
    """Headline numbers of every virtual service, for list columns."""

    available: bool
    reason: str | None = None
    window_s: int
    items: list[MetricsOverviewItem]


class AccessLogs(ApiModel):
    """A page of access log lines, newest first."""

    available: bool
    reason: str | None = None
    entries: list[AccessLogEntry]
    next_cursor: str | None = Field(
        default=None, description="Pass back as `cursor` for older lines; null at the end."
    )


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


class CertMaterialIn(ApiModel):
    """Certificate material: send ``pem`` or ``pkcs12_b64`` with ``password``."""

    pem: str | None = Field(
        default=None,
        max_length=65536,
        description="Certificate chain followed by the private key (PEM).",
    )
    pkcs12_b64: str | None = Field(
        default=None,
        max_length=262144,
        description="Base64 of a PKCS#12/PFX file holding the leaf, chain and key.",
    )
    password: SecretStr | None = Field(
        default=None,
        max_length=1024,
        description="Password of the PKCS#12 file; never logged or stored. Empty if none.",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "CertMaterialIn":
        if (self.pem is None) == (self.pkcs12_b64 is None):
            raise ValueError("send exactly one of pem or pkcs12_b64")
        if self.pem is not None and self.password is not None:
            raise ValueError("password only applies to pkcs12_b64")
        return self


class CertImportIn(CertMaterialIn):
    """Body of ``POST /certificates/import``."""

    name: str


class CertReplaceIn(CertMaterialIn):
    """Body of ``POST /certificates/{id}/replace``: new material for an existing certificate."""


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
    quotas: dict[str, int] = Field(default_factory=dict)
    require_approval: bool = False


class TenantPatch(ApiModel):
    """Change a tenant; send only the fields to change."""

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

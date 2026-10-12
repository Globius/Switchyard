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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    create_model,
    model_validator,
)

from switchyard.domain import models as dm
from switchyard.domain.changes import Action, DraftStatus, ObjectType
from switchyard.domain.snapshot import Snapshot
from switchyard.domain.telemetry import (
    AccessLogEntry,
    EngineMetrics,
    MetricSummary,
    ServiceMetrics,
)
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
    ObjectType.POLICY_SET: (
        dm.RedirectPolicySet,
        dm.AllowListPolicySet,
        dm.RewritePolicySet,
        dm.RespondPolicySet,
        dm.ErrorPagesPolicySet,
        dm.RateLimitPolicySet,
        dm.WafPolicySet,
        dm.ProtectionPolicySet,
        dm.AccessPolicySet,
        dm.JwtPolicySet,
        dm.ClientCertPolicySet,
    ),
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
    members: int = Field(
        default=0,
        description="People with a grant in the tenant: signed-in users whose provider groups "
        "match one of the tenant's role mappings.",
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


class EngineAddress(ApiModel):
    """One address the deploy job gave an engine's NIC on a network."""

    network_id: UUID
    address: str = Field(description="IP address without a prefix length (the network's applies).")


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
    guest_kind: Literal["qemu", "lxc"] | None = Field(
        default=None,
        description="`qemu` (a VM) or `lxc` (a container), as deployed; null for a hand-built "
        "engine (show nothing, or a generic icon).",
    )
    addresses: list[EngineAddress] = Field(
        default_factory=list,
        description="Addresses the deploy job gave the engine's VIP and backend NICs, one per "
        "addressed NIC (the management address is `mgmt_ip`). Empty for a hand-built engine or a "
        "NIC deployed without `nic_pools`.",
    )
    agent_enrolled: bool = Field(
        description="True once `sy-agent enroll` has traded a token for a client certificate."
    )
    agent_cert_serial: str | None = Field(
        default=None, description="Serial of the agent's client certificate."
    )
    host_online: bool | None = Field(
        default=None,
        description="Whether Proxmox reports the engine's node online; null when the engine has "
        "no node or Proxmox is not configured or not reachable.",
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


class SystemSettingsRead(ApiModel):
    """Controller-wide settings as they are in effect."""

    allow_lxc_guests: bool = Field(
        description="Whether SE groups may be deployed as containers (LXC). Off by default: a "
        "privileged container shares the Proxmox host's kernel."
    )
    allow_lxc_guests_forced: bool = Field(
        description="True when the deployment forces it on (`SWITCHYARD_ALLOW_LXC=1`); the stored "
        "value then has no effect."
    )


class SystemSettingsIn(ApiModel):
    """Change settings; send only the ones to change."""

    allow_lxc_guests: bool | None = None


class MeFeatures(ApiModel):
    """Optional features, so the UI can show or hide controls."""

    lxc_guests: bool = Field(
        default=False, description="Container (LXC) guests may be deployed (`allow_lxc_guests`)."
    )


class MeLinks(ApiModel):
    """Where the UI may link to, set by the operator of the controller."""

    grafana: str | None = Field(
        default=None,
        description="Base URL of Grafana (`SWITCHYARD_GRAFANA_URL`), or null when none is set.",
    )
    grafana_embedded: bool = Field(
        default=False,
        description="Grafana is behind the controller: `POST /tenants/{t}/grafana/launch` returns "
        "a link that opens it in a new tab, scoped to the tenant (ADR 0085).",
    )


class GrafanaLaunchIn(ApiModel):
    """Where to land in the embedded Grafana."""

    landing: str = Field(
        default="",
        max_length=400,
        description="A path inside Grafana: empty (the tenant's home dashboard), "
        "`/d/<uid>[/<slug>]`, `/explore` or `/dashboards`, optionally with a query string.",
    )


class GrafanaLaunchOut(ApiModel):
    """A one-time link to the embedded Grafana."""

    url: str = Field(
        description="Open this path in a new browser tab within `expires_at`. It works "
        "once: Switchyard exchanges it for a short session cookie and redirects into Grafana."
    )
    expires_at: datetime = Field(description="When the link stops working if it was not opened.")
    tenant_id: UUID | None = Field(description="The tenant Grafana is scoped to; null for all.")


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
    links: MeLinks = Field(default_factory=MeLinks)
    features: MeFeatures = Field(default_factory=MeFeatures)


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
    "acme_provider",
    "application",
    "break_glass",
    "certificate",
    "change_set",
    "controller",
    "dns_provider",
    "draft",
    "engine",
    "enrollment_token",
    "identity_provider",
    "infrastructure",
    "job",
    "job_own",
    "kubernetes_cluster",
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


class SdnApplyRead(ApiModel):
    """Where the Proxmox SDN apply stands for one managed network.

    An apply is tenant-wide: while one is `queued` or `running` it counts for every managed
    network. After it, `succeeded` and `failed` come from the latest finished apply, and `never`
    marks a network that no finished apply has included yet.
    """

    network_id: UUID
    network: str
    state: Literal["queued", "running", "succeeded", "failed", "never"]
    job_id: UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class ServiceEngineRead(ApiModel):
    """An engine a virtual service is placed on (the enabled engines of its SE group)."""

    id: UUID
    name: str
    mgmt_ip: str | None = None
    state: EngineState


class EngineFileRead(ApiModel):
    """One rendered file."""

    file: str = Field(description="Path relative to /etc/switchyard/ on the SE.")
    scope: Literal["group", "engine"] = Field(
        description="`group`: the same on every engine of the SE group (HAProxy). `engine`: this "
        "engine's own routing files (FRR or Keepalived)."
    )
    language: Literal["haproxy", "plaintext"]
    content: str


class EngineRenderedRead(ApiModel):
    """Everything the controller renders for one engine of a virtual service's SE group.

    Certificates and keys are never included, and BGP passwords in `frr/frr.conf` read `REDACTED`.
    """

    engine: str
    engine_id: UUID
    se_group_id: UUID
    fingerprint: str = Field(description="Render fingerprint of the group's HAProxy files.")
    files: list[EngineFileRead]


class RunningHaproxyRead(ApiModel):
    """HAProxy's running configuration, from the Data Plane API."""

    content: str
    fingerprint: str | None = Field(description="Fingerprint stamped in the running config.")
    desired_fingerprint: str | None = Field(description="What the desired state renders to.")
    fingerprint_matches: bool = Field(description="The stamped fingerprints are equal.")
    matches_desired: bool = Field(
        description="The stamps are equal and the text is the same as the desired file, so a "
        "hand edit that kept the stamp shows as `false`."
    )


class RunningAgentFileRead(ApiModel):
    """One routing or certificate file on the engine, by hash."""

    path: str
    sha256: str | None = Field(description="Hash the agent reports; null if absent on the engine.")
    desired_sha256: str | None = Field(description="Hash of what the controller would send now.")
    matches_desired: bool


class RunningAgentRead(ApiModel):
    """What the agent reports. It has no call that reads file contents, only hashes."""

    bundle_id: str | None
    desired_bundle_id: str | None
    files: list[RunningAgentFileRead]


class RunningConfigRead(ApiModel):
    """What one engine is running, compared with the desired state.

    `haproxy` is null when the engine could not be reached or has no configuration, `agent` is null
    when the engine has no agent. For tenants `error` is a fixed sentence.
    """

    engine: str
    engine_id: UUID
    se_group_id: UUID
    reachable: bool
    error: str | None = None
    haproxy: RunningHaproxyRead | None = None
    agent: RunningAgentRead | None = None
    fetched_at: datetime


class PermissionRead(ApiModel):
    """One cell of the permission matrix. `verb` and `object` are published as enums."""

    verb: PermissionVerb
    object: PermissionObject
    min_role: Role = Field(description="The weakest role that may do this.")
    description: str = Field(description="One sentence for tooltips.")
    group: str = Field(
        description="The group the role matrix screen shows it under: `Read`, `Applications`, "
        "`Infrastructure`, `Change sets`, `Runtime`, `Tenant settings`, `Access` or `System`."
    )


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
    message: str = Field(default="", description="The message given when it was applied.")
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
    check_status: str | None = Field(
        default=None,
        description="Short code of the last health check (`L4OK`, `L7OK`, `L7STS`, `L4TOUT`, ...).",
    )
    last_change_s: int | None = Field(
        default=None, description="Seconds since the member's status last changed."
    )


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


class PrincipalNameRead(ApiModel):
    """A principal label (`actor`, `author`, `approver`) with a name for people."""

    actor: str = Field(description="The label as given, for example `user:3f9a`, `token:ci-bot`.")
    kind: str = Field(description="`user`, `token`, `local`, `login`, `system` or `unknown`.")
    display_name: str = Field(description="Never empty; users get the name their provider gave.")


class DiscoveryGroup(ApiModel):
    """One Prometheus HTTP service-discovery target group (the format Prometheus reads)."""

    targets: list[str] = Field(description="`host:port` of the exporter.")
    labels: dict[str, str] = Field(
        description="`tenant` (id), `tenant_name`, `se_group`, `engine` and `vs` (the group's "
        "service names between commas, for example `,shop,www,`)."
    )


class HostStorageRead(ApiModel):
    """A storage of a Proxmox node. Sizes are bytes."""

    name: str
    kind: str = Field(description="Storage type, for example `lvmthin`, `zfspool`, `nfs`.")
    active: bool
    content: list[str] = Field(description="What the storage holds, for example `images`.")
    total: int | None = None
    used: int | None = None
    available: int | None = None


class HostEngineRead(ApiModel):
    """An engine of the SE group on a node."""

    id: UUID
    name: str
    vmid: int | None = None
    state: EngineState


class HostNodeRead(ApiModel):
    """A Proxmox node and the SE group's engines on it."""

    name: str
    online: bool | None = Field(
        description="As the cluster reports it; null when Proxmox could not be asked."
    )
    cpu: float | None = Field(default=None, description="CPU load, 0 to 1.")
    max_cpu: int | None = None
    mem_used: int | None = Field(default=None, description="Bytes.")
    mem_total: int | None = Field(default=None, description="Bytes.")
    uptime_s: int | None = None
    storage: list[HostStorageRead]
    engines: list[HostEngineRead]


class PlacementRead(ApiModel):
    """Where an SE group's engines run on the Proxmox cluster.

    With `available: false` (Proxmox not configured or not reachable) `nodes` lists only the nodes
    the group's engines are placed on, with `online: null` and no capacity.
    """

    available: bool
    reason: str | None = None
    nodes: list[HostNodeRead]


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
    engine_addresses: list[str] = Field(
        default_factory=list,
        description="Addresses in the pool that engines hold (a management or data NIC). They "
        "count as used and are never offered as `next_free`.",
    )
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


class EngineMetricsRead(ApiModel):
    """Metrics of one service engine, or why there are none."""

    available: bool = Field(
        description="False when no metrics backend is configured or it cannot be reached."
    )
    reason: str | None = Field(default=None, description="Why `available` is false.")
    metrics: EngineMetrics | None = None


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


class CaImportIn(ApiModel):
    """Body of `POST /certificates/import-ca`: a client CA bundle for mutual TLS."""

    name: str
    pem: str = Field(max_length=65536, description="The CA certificates (PEM), no private key.")
    crl_pem: str | None = Field(
        default=None, max_length=1048576, description="Optional certificate revocation list (PEM)."
    )


class CertReplaceIn(CertMaterialIn):
    """Body of ``POST /certificates/{id}/replace``: new material for an existing certificate."""


class CertIssueIn(ApiModel):
    """Body of ``POST /certificates/issue``."""

    name: str
    source: Literal["acme", "cloudflare_origin"]
    sans: list[str] = Field(min_length=1, max_length=50)
    acme_provider: str | None = Field(
        default=None,
        description="ACME provider name (`acme` only). The default provider when empty.",
    )
    challenge: Literal["dns-01", "http-01", "tls-alpn-01"] | None = Field(
        default=None,
        description="`dns-01` (default), `http-01` or `tls-alpn-01`. The last two cannot validate "
        "wildcard names. TLS-ALPN-01 answers every HTTPS handshake for the name with the "
        "challenge certificate while it runs, so a name that is serving traffic is interrupted "
        "for a few seconds (see `warnings` in the check).",
    )
    dns_provider: str | None = Field(
        default=None,
        description="DNS credential name for `dns-01`. Chosen by zone when empty.",
    )
    renew_policy: Literal["auto", "manual"] | None = Field(
        default=None, description="Defaults to `auto` for a new certificate."
    )
    renew_before_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description="How long before expiry an automatic renewal starts.",
    )


#: A Proxmox node name: it becomes a path segment of every Proxmox call.
NODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$"


class DeployIn(ApiModel):
    """Body of ``POST /se-groups/{id}/deploy``."""

    count: int = Field(ge=1, le=16, description="Total engines the group should have afterwards.")
    nodes: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")]] = (
        Field(min_length=1, description="Proxmox nodes to spread new VMs over (node names).")
    )
    template_vmid: int = Field(ge=100)
    mgmt_pool_id: UUID
    pool: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    credentials_ref: str | None = None
    ssh_public_keys: str | None = Field(default=None, max_length=8192)
    nic_pools: dict[UUID, UUID] | None = Field(
        default=None,
        description="Address pool for each VIP or backend network the group attaches, keyed by "
        "network id (each pool must be on that network). Every new engine gets one address per "
        "entry on that NIC; a network without an entry leaves its NIC unaddressed (set it up by "
        "hand).",
    )
    guest_kind: Literal["qemu", "lxc"] = Field(
        default="qemu",
        description="`qemu` clones a cloud-init VM template; `lxc` clones a container template "
        "(lighter, no cloud-init: enrollment material must be delivered out of band).",
    )


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
    message: str | None = Field(
        default=None,
        max_length=2000,
        description="Revision message, kept on the revision and in the Git mirror commit.",
    )


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


# -- ACME providers and DNS credentials (ADR 0085) -------------------------------------------------


class DnsFieldRead(ApiModel):
    """One field of a DNS provider type's form."""

    key: str = Field(description="Name in `config`, or in `credentials` when `secret`.")
    label: str
    kind: Literal["text", "select", "textarea"]
    required: bool
    secret: bool = Field(description="Write-only: sent in `credentials`, never returned.")
    help: str
    options: list[str] = Field(description="Allowed values of a `select`.")
    default: str | None = Field(description="Used when the field is left empty.")


class DnsProviderTypeRead(ApiModel):
    """A kind of DNS provider and the form it needs."""

    type: str
    label: str
    fields: list[DnsFieldRead]
    supports_zone_discovery: bool = Field(
        description="The provider finds the zone of a name itself; `zones` then only chooses "
        "between credentials."
    )


class DnsProviderIn(ApiModel):
    """Create a DNS credential."""

    name: str
    type: str = Field(description="One of the types from `GET /dns-provider-types`.")
    zones: list[str] = Field(
        default_factory=list,
        description="Domains this credential serves, for example `example.test`. The longest "
        "match wins; a credential without zones serves every name nobody else claims.",
    )
    config: dict[str, str] = Field(default_factory=dict, description="The non-secret fields.")
    credentials: dict[str, str] = Field(
        default_factory=dict, description="The secret fields. Stored in the secrets backend."
    )
    enabled: bool = True


class DnsProviderPatch(ApiModel):
    """Change a DNS credential; send only what changes."""

    zones: list[str] | None = None
    config: dict[str, str] | None = Field(
        default=None, description="Settings to set (merged with the stored ones)."
    )
    credentials: dict[str, str] | None = Field(
        default=None,
        description="Secret fields to replace; the others keep their stored value.",
    )
    enabled: bool | None = None


class DnsProviderRead(ApiModel):
    """A DNS credential (never any secret)."""

    id: UUID
    name: str
    type: str
    zones: list[str]
    config: dict[str, str]
    credentials_set: bool
    enabled: bool
    version: int


class DnsProviderTestIn(ApiModel):
    """Optionally name the zone to test in."""

    zone: str | None = Field(
        default=None,
        description="Test only this zone. By default every zone of the credential is tested.",
    )


class DnsZoneTestRead(ApiModel):
    """The test in one zone."""

    zone: str
    ok: bool
    detail: str


class DnsProviderTestRead(ApiModel):
    """Result of publishing and removing a test TXT record in each zone of a credential."""

    ok: bool
    detail: str
    zones: list[DnsZoneTestRead]


class DnsCandidateRead(ApiModel):
    """A credential that could publish records for a name."""

    id: UUID
    name: str
    type: str
    zone: str | None = Field(
        description="The zone that matched; empty for a credential without a zone list."
    )


class DnsNameMatchRead(ApiModel):
    """The credentials that cover one name, best first."""

    name: str
    candidates: list[DnsCandidateRead]


class AcmePresetRead(ApiModel):
    """A well-known certificate authority."""

    key: str
    label: str
    directory_url: str | None = Field(description="Empty when the administrator must enter it.")
    auth_mode: Literal["none", "eab", "oauth"]
    eab_required: bool
    note: str


class AcmeOauthIn(ApiModel):
    """OAuth settings for a CA behind an identity provider (client-credentials grant)."""

    token_url: str | None = None
    client_id: str | None = None
    scope: str | None = None
    audience: str | None = None
    bearer_on_acme: bool | None = Field(
        default=None, description="Send the token on every ACME request."
    )
    eab_url: str | None = Field(
        default=None,
        description="Endpoint that returns a fresh EAB key (JSON with a key id and an HMAC key) "
        "when called with the bearer token.",
    )


class AcmeOauthRead(ApiModel):
    """The stored OAuth settings (the client secret is never returned)."""

    token_url: str | None = None
    client_id: str | None = None
    scope: str | None = None
    audience: str | None = None
    bearer_on_acme: bool | None = None
    eab_url: str | None = None


class AcmeProviderIn(ApiModel):
    """Register an ACME provider."""

    name: str
    preset: str = Field(default="custom", description="A key from `GET /acme-provider-presets`.")
    directory_url: str | None = Field(
        default=None, description="Required for presets without a fixed directory."
    )
    email: str | None = Field(default=None, description="Contact address for the ACME account.")
    auth_mode: Literal["none", "eab", "oauth"] | None = Field(
        default=None, description="Defaults to what the preset usually needs."
    )
    eab_kid: str | None = None
    eab_hmac_key: str | None = Field(
        default=None, description="Base64url HMAC key of the EAB credentials. Write-only."
    )
    oauth: AcmeOauthIn | None = None
    oauth_client_secret: str | None = Field(default=None, description="Write-only.")
    account_key: str | None = Field(
        default=None,
        description="Write-only PEM of an RSA account key (2048 bits or more) to use instead of "
        "a generated one.",
    )
    ca_bundle_pem: str | None = Field(
        default=None,
        description="PEM certificates of extra trusted roots, for a private CA. Public data.",
    )
    terms_agreed: bool = Field(
        default=False, description="Must be true: the controller accepts the CA's terms."
    )
    enabled: bool = True
    default: bool = Field(
        default=False, description="Use this provider when a certificate names none."
    )


class AcmeProviderPatch(ApiModel):
    """Change an ACME provider; send only what changes."""

    directory_url: str | None = None
    email: str | None = None
    auth_mode: Literal["none", "eab", "oauth"] | None = None
    eab_kid: str | None = None
    eab_hmac_key: str | None = None
    oauth: AcmeOauthIn | None = None
    oauth_client_secret: str | None = None
    account_key: str | None = Field(
        default=None, description="Replaces the account key; the account is registered again."
    )
    ca_bundle_pem: str | None = Field(
        default=None, description="Replaces the extra trusted roots; empty removes them."
    )
    enabled: bool | None = None
    default: bool | None = None


class AcmeProviderRead(ApiModel):
    """An ACME provider (never any secret)."""

    id: UUID
    name: str
    preset: str
    directory_url: str
    email: str | None
    auth_mode: Literal["none", "eab", "oauth"]
    eab_kid: str | None
    eab_configured: bool
    oauth: AcmeOauthRead | None
    oauth_secret_set: bool
    account_registered: bool = Field(
        description="True after a successful test (the CA accepted the account)."
    )
    ca_bundle_set: bool = Field(description="Extra trusted roots are configured.")
    terms_agreed: bool
    enabled: bool
    default: bool
    used_by: int = Field(description="Certificates that name this provider.")
    version: int


class AcmeTestStageRead(ApiModel):
    """One step of a connection test."""

    name: Literal["directory", "eab", "account"]
    ok: bool
    detail: str


class AcmeProviderTestRead(ApiModel):
    """Result of testing a provider: the directory, then the EAB credentials, then the account."""

    ok: bool
    detail: str
    stages: list[AcmeTestStageRead] = Field(
        description="The steps in order; the list stops at the first one that fails."
    )
    tos_url: str | None
    external_account_required: bool


class IssueProblemRead(ApiModel):
    """One finding of a certificate request check."""

    path: str = Field(description="Field path, for example `sans[1]` or `acme_provider`.")
    message: str


class IssueCheckRead(ApiModel):
    """What is wrong with a certificate request, if anything."""

    ok: bool
    problems: list[IssueProblemRead]
    warnings: list[IssueProblemRead] = Field(
        description="Things to know that do not stop the request."
    )


class RenewalRead(ApiModel):
    """One issuance or renewal attempt."""

    id: UUID
    certificate: str
    trigger: Literal["manual", "auto"]
    started_at: datetime
    finished_at: datetime
    ok: bool
    error: str | None = Field(description="The CA's or provider's error, quoted.")
    change_set_id: UUID | None = Field(description="The `system:certs` change set it opened.")
    not_after: datetime | None = Field(description="Expiry of the certificate it obtained.")
    acme_provider: str | None
    challenge: str | None


class RenewalStatusRead(ApiModel):
    """When a certificate renews and how the last attempts went."""

    certificate_id: UUID
    certificate: str
    source: str
    issuer: str = Field(description="The ACME provider, or the kind of source.")
    not_after: datetime
    renew_policy: Literal["auto", "manual"]
    renew_before_days: int
    renew_due_at: datetime = Field(description="`not_after` minus `renew_before_days`.")
    last_attempt_at: datetime | None
    last_ok: bool | None
    last_error: str | None
    last_success_at: datetime | None
    failures_in_a_row: int


# -- The controller's own address (ADR 0081) ------------------------------------------------------


class ControllerPortRead(ApiModel):
    """A port of the `controller` service."""

    port: int
    tls: bool
    https_redirect_port: int | None


class ControllerListenRead(ApiModel):
    """Where the `controller` service answers."""

    vip: str
    ports: list[ControllerPortRead]


class ControllerCertificateRead(ApiModel):
    """The certificate the `controller` service presents."""

    name: str
    sans: list[str]
    not_after: datetime


class ControllerStateRead(ApiModel):
    """The settings before or after a change."""

    fqdn: str | None
    public_url: str | None
    vip: str | None = None
    certificate: str | None = None


class ControllerChangeRead(ApiModel):
    """A change that waits for confirmation (or is still being applied)."""

    state: Literal["applying", "waiting"]
    proposed_by: str
    proposed_at: datetime
    window_s: int
    expires_at: datetime | None = Field(description="Set once the change set has applied.")
    seconds_left: int | None
    before: ControllerStateRead
    after: ControllerStateRead
    change_set_id: UUID | None = Field(description="The management tenant's change set.")
    revision_before: int | None
    revision_after: int | None


class ControllerResultRead(ApiModel):
    """How the previous change ended."""

    kind: Literal["confirmed", "reverted", "failed"]
    at: datetime
    by: str | None
    reason: str | None = None
    fqdn: str | None = None
    restored: bool | None = None


class IdpRedirectRead(ApiModel):
    """The address an identity provider must allow for the new FQDN."""

    provider: str
    protocol: str
    redirect_url: str


class ControllerRead(ApiModel):
    """The controller's address settings."""

    managed: bool = Field(
        description="The management tenant and its `controller` service exist. Without them only "
        "the FQDN (public URL) can be set."
    )
    fqdn: str | None
    public_url: str | None
    listen: ControllerListenRead | None
    certificate: ControllerCertificateRead | None
    pending: ControllerChangeRead | None
    last_result: ControllerResultRead | None
    redirect_urls: list[IdpRedirectRead] = Field(
        description="For a pending change: what each identity provider must allow first."
    )
    break_glass: str = Field(description="How to recover without the UI.")


class ControllerChangeIn(ApiModel):
    """Propose a change to the controller's address."""

    fqdn: str = Field(description="The host name users will open.")
    vip: str | None = Field(default=None, description="A new VIP for the `controller` service.")
    certificate: str | None = Field(
        default=None, description="Name of a server certificate in the management tenant."
    )
    confirm_within_s: int | None = Field(
        default=None,
        ge=30,
        le=600,
        description="Seconds to confirm before the change is reverted (default 120).",
    )


# -- Importing an Avi configuration (ADR 0150) ----------------------------------------------------


class AviImportIn(ApiModel):
    """Convert an Avi configuration export into a draft change set."""

    export: dict[str, Any] = Field(
        description="The decoded Avi configuration export (`POST /api/configuration/export`, or "
        "the `avi_config.json` of a backup): an object with a list per object type."
    )
    se_group: str = Field(description="Name of the SE group the services will use.")
    vip_pool: str | None = Field(
        default=None, description="VIP pool the new VIPs are taken from, one per Avi VIP."
    )
    vip_map: dict[str, str] = Field(
        default_factory=dict,
        description="Fixed new addresses, keyed by the Avi VIP name or its old address.",
    )
    prefix: str = Field(default="", max_length=20, description="Put in front of every new name.")
    include_unused: bool = Field(
        default=False, description="Also convert pools and other objects no service uses."
    )
    only: list[str] = Field(
        default_factory=list, description="Convert only these virtual services (Avi names)."
    )
    dry_run: bool = Field(
        default=True,
        description="Only report what would be created. Send `false` to create the draft.",
    )


class AviMappedRead(ApiModel):
    """An Avi object and what it became."""

    kind: str
    avi_name: str
    name: str


class AviVipRead(ApiModel):
    """An Avi VIP and the address that replaces it."""

    avi_vsvip: str
    address: str | None = Field(description="The address in the export.")
    new_address: str | None = Field(description="The address it gets here, if one was found.")
    services: list[str]


class AviFindingRead(ApiModel):
    """One decision of the conversion."""

    level: Literal["info", "warning", "manual", "skipped"]
    object: str
    message: str


class AviImportRead(ApiModel):
    """What the import did or would do."""

    dry_run: bool
    change_set_id: UUID | None = Field(description="The draft change set; empty for a dry run.")
    counts: dict[str, int]
    objects: list[AviMappedRead]
    vips: list[AviVipRead]
    findings: list[AviFindingRead]


# -- Kubernetes clusters (ADR 0151) ---------------------------------------------------------------


class KubernetesClusterIn(ApiModel):
    """Connect a Kubernetes cluster."""

    name: str
    api_url: str = Field(description="`https://host:port` of the API server.")
    token: str = Field(
        description="A service-account token with the access in docs/kubernetes.md. Write-only."
    )
    se_group: str = Field(description="Name of the SE group the services use.")
    vip_pool: str = Field(description="Name of the VIP pool addresses are taken from.")
    ca_pem: str | None = Field(default=None, description="PEM of the API server's CA, if private.")
    mode: Literal["nodeport", "pods"] = Field(
        default="nodeport",
        description="`nodeport`: pools hold the ready nodes at the NodePort. `pods`: pools hold "
        "the ready endpoints (the SEs must reach the pod network).",
    )
    class_name: str = Field(
        default="switchyard.io/lb", description="`loadBalancerClass` of the Services that are ours."
    )
    gateway_class: str = Field(
        default="switchyard", description="`gatewayClassName` of the Gateways that are ours."
    )
    enabled: bool = True


class KubernetesClusterPatch(ApiModel):
    """Change a cluster; send only what changes."""

    api_url: str | None = None
    token: str | None = Field(default=None, description="Replaces the stored token.")
    ca_pem: str | None = None
    se_group: str | None = None
    vip_pool: str | None = None
    mode: Literal["nodeport", "pods"] | None = None
    class_name: str | None = None
    gateway_class: str | None = None
    enabled: bool | None = None


class KubernetesSyncRead(ApiModel):
    """The result of the last sync."""

    planned_services: int | None = None
    planned_pools: int | None = None
    created: int | None = None
    updated: int | None = None
    deleted: int | None = None
    change_set_id: UUID | None = None
    addresses_written: int | None = None
    skipped: str | None = None
    notes: list[str] | None = None


class KubernetesClusterRead(ApiModel):
    """A connected cluster (never the token)."""

    id: UUID
    name: str
    api_url: str
    ca_set: bool
    se_group: str
    vip_pool: str
    mode: Literal["nodeport", "pods"]
    class_name: str
    gateway_class: str
    enabled: bool
    waiting_for_change_set: UUID | None = Field(
        description="The sync waits for this change set to finish before it plans again."
    )
    last_sync_at: datetime | None
    last_sync_ok: bool | None
    last_sync_error: str | None
    last_sync: KubernetesSyncRead | None
    version: int


class KubernetesTestRead(ApiModel):
    """What a connection test found."""

    ok: bool
    detail: str
    version: str | None
    nodes: int | None
    services: int | None
    gateway_api: bool | None = Field(description="The Gateway API CRDs are installed.")
    permissions: dict[str, bool]
    problems: list[str]

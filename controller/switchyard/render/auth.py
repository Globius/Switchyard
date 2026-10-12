"""Render the authentication proxy side of an access policy set (ADR 0130 to 0132).

An *auth instance* is one access set used by one virtual service. Each instance has

- a proxy listener on ``127.0.0.1:<auth_port>`` (oauth2-proxy; the engine runs one process per
  config file under ``/etc/switchyard/auth/``), and
- a loopback re-entry bind on the service's HAProxy frontend, ``127.0.0.1:<loop_port>``, which is
  where the proxy sends a request once it has authenticated it.

Both ports are derived from the service's position among the group's access services (sorted by
name), so the HAProxy renderer and this module agree without sharing state. The functions here are
pure; secrets arrive resolved from OpenBao and only ever land in ``auth/*.cfg``, a file the agent
writes with owner-only permissions.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot
from switchyard.render import escape as esc
from switchyard.render.broker import check_broker_url
from switchyard.render.escape import RenderError

#: First port of the proxy listeners (127.0.0.1 only).
AUTH_PORT_BASE = 14000
#: First port of the loopback re-entry binds (127.0.0.1 only).
LOOP_PORT_BASE = 18000
#: Instances one SE group can have (the port ranges are this long).
MAX_INSTANCES = 1000

_SECRET = re.compile(r"[A-Za-z0-9._~+/=-]{8,512}")


@dataclass(frozen=True)
class AuthInstance:
    """One access set as used by one service.

    Attributes:
        name: File and backend name, ``<service>-<set>``.
        service: The virtual service.
        policy: The access set.
        auth_port: Loopback port the proxy listens on.
        loop_port: Loopback port of the service's re-entry bind.
    """

    name: str
    service: dm.VirtualService
    policy: dm.AccessPolicySet
    auth_port: int
    loop_port: int
    tenant: str = ""
    directory: dm.DirectoryPolicySet | None = None

    def issuer(self, broker_url: str | None) -> str:
        """The OIDC issuer: the set's own, or the login broker's for its directory."""
        oidc = self.policy.oidc
        if oidc.issuer is not None:
            return oidc.issuer
        if self.directory is None:
            raise RenderError(f"access set {self.policy.name} names a directory that is missing")
        if not broker_url:
            raise RenderError(
                f"access set {self.policy.name} uses directory {self.directory.name} "
                "but no login broker URL is configured (SWITCHYARD_AUTH_BROKER_URL)"
            )
        base = check_broker_url(broker_url)
        return f"{base}/{esc.ident(self.tenant)}/{esc.ident(self.directory.name)}"

    @property
    def backend(self) -> str:
        """Name of the HAProxy backend that points at the proxy."""
        return f"auth_{self.name}"

    @property
    def path(self) -> str:
        """Bundle path of the proxy configuration."""
        return f"auth/{self.name}.cfg"

    @property
    def env_path(self) -> str:
        """Bundle path of the file that holds the proxy's secrets."""
        return f"auth/{self.name}.env"


def access_set_of(snapshot: Snapshot, svc: dm.VirtualService) -> dm.AccessPolicySet | None:
    """The access set a service attaches, if any (validation allows at most one)."""
    policies = {p.id: p for p in snapshot.policy_sets}
    for pid in svc.policy_set_ids:
        policy = policies.get(pid)
        if isinstance(policy, dm.AccessPolicySet):
            return policy
    return None


def auth_instances(snapshot: Snapshot, se_group_id: UUID) -> list[AuthInstance]:
    """The auth instances of an SE group's enabled HTTP services, in service-name order.

    Example:
        >>> from switchyard.domain.snapshot import Snapshot
        >>> auth_instances(Snapshot(), UUID(int=1))
        []
    """
    services = sorted(
        (
            s
            for s in snapshot.virtual_services
            if s.se_group_id == se_group_id and s.enabled and s.protocol is dm.Protocol.HTTP
        ),
        key=lambda s: s.name,
    )
    out: list[AuthInstance] = []
    tenants = {t.id: t.name for t in snapshot.tenants}
    directories = {p.id: p for p in snapshot.policy_sets if isinstance(p, dm.DirectoryPolicySet)}
    for svc in services:
        policy = access_set_of(snapshot, svc)
        if policy is None:
            continue
        index = len(out)
        if index >= MAX_INSTANCES:
            raise RenderError(f"SE group has more than {MAX_INSTANCES} services with a login")
        name = f"{esc.ident(svc.name)}-{esc.ident(policy.name)}"
        directory = directories.get(policy.oidc.directory_id) if policy.oidc.directory_id else None
        out.append(
            AuthInstance(
                name,
                svc,
                policy,
                AUTH_PORT_BASE + index,
                LOOP_PORT_BASE + index,
                tenants.get(svc.tenant_id, ""),
                directory,
            )
        )
    return out


def _secret(value: str, what: str) -> str:
    if not _SECRET.fullmatch(value):
        raise RenderError(f"{what} is empty, too short or has characters that are not allowed")
    return value


def _quoted(value: str) -> str:
    """A TOML basic string; the validators keep ``"``, backslash and control characters out."""
    if any(c in value for c in '"\\\n\r\t\x00'):
        raise RenderError(f"unsafe value {value!r}")
    return f'"{value}"'


def render_env(inst: AuthInstance, secrets: Mapping[str, str]) -> str:
    """The secrets of one instance as an ``EnvironmentFile`` (oauth2-proxy reads these variables).

    ``secrets`` maps ``secret_ref`` to the resolved value; a missing reference is an error
    because an unauthenticated proxy must never start.

    Example:
        >>> import uuid
        >>> from switchyard.domain.models import AccessPolicySet, OidcSettings, VirtualService
        >>> oidc = OidcSettings(issuer="https://login.example.test", client_id="web",
        ...     client_secret_ref="kv/tenants/a/c", cookie_secret_ref="kv/tenants/a/k")
        >>> policy = AccessPolicySet(id=uuid.UUID(int=1), tenant_id=uuid.UUID(int=2), name="sso",
        ...     oidc=oidc)
        >>> svc = VirtualService.model_construct(name="web")
        >>> text = render_env(AuthInstance("web-sso", svc, policy, 14000, 18000),
        ...     {"kv/tenants/a/c": "client-secret", "kv/tenants/a/k": "0123456789abcdef"})
        >>> text.splitlines()[0]
        'OAUTH2_PROXY_CLIENT_SECRET=client-secret'
    """
    oidc = inst.policy.oidc
    for ref in (oidc.client_secret_ref, oidc.cookie_secret_ref):
        if ref not in secrets:
            raise RenderError(f"missing secret {ref} for access set {inst.policy.name}")
    cookie_secret = _secret(secrets[oidc.cookie_secret_ref], "the cookie secret")
    if len(cookie_secret.encode()) not in (16, 24, 32):
        raise RenderError("the cookie secret must be 16, 24 or 32 bytes")
    client_secret = _secret(secrets[oidc.client_secret_ref], "the client secret")
    lines = [
        f"OAUTH2_PROXY_CLIENT_SECRET={client_secret}",
        f"OAUTH2_PROXY_COOKIE_SECRET={cookie_secret}",
    ]
    return "\n".join(lines) + "\n"


def render_instance(inst: AuthInstance, broker_url: str | None = None) -> str:
    """The oauth2-proxy configuration of one instance, without its secrets.

    The secrets are in :func:`render_env`; the unit reads both. The file is not secret, so the
    agent keeps it group-readable for the proxy's user.

    Example:
        >>> import uuid
        >>> from switchyard.domain.models import AccessPolicySet, OidcSettings, VirtualService
        >>> oidc = OidcSettings(issuer="https://login.example.test", client_id="web",
        ...     client_secret_ref="kv/tenants/a/c", cookie_secret_ref="kv/tenants/a/k")
        >>> policy = AccessPolicySet(id=uuid.UUID(int=1), tenant_id=uuid.UUID(int=2), name="sso",
        ...     oidc=oidc)
        >>> svc = VirtualService.model_construct(name="web")
        >>> text = render_instance(AuthInstance("web-sso", svc, policy, 14000, 18000))
        >>> 'http_address = "127.0.0.1:14000"' in text
        True
    """
    oidc = inst.policy.oidc
    wants_token = inst.policy.identity_headers.token is not None
    lines = [
        "# Managed by Switchyard. Do not edit: changes are reported as drift.",
        f"# access set {esc.ident(inst.policy.name)} on service {esc.ident(inst.service.name)}",
        'provider = "oidc"',
        f"oidc_issuer_url = {_quoted(inst.issuer(broker_url))}",
        f"client_id = {_quoted(oidc.client_id)}",
        f"cookie_name = {_quoted(oidc.cookie_name)}",
        "cookie_secure = true",
        "cookie_httponly = true",
        'cookie_samesite = "lax"',
        f'cookie_expire = "{oidc.cookie_expire_s}s"',
        f'cookie_refresh = "{oidc.cookie_refresh_s}s"',
        f"scope = {_quoted(' '.join(oidc.scopes))}",
        f"oidc_groups_claim = {_quoted(oidc.groups_claim)}",
        'email_domains = ["*"]',
        'code_challenge_method = "S256"',
        f'http_address = "127.0.0.1:{inst.auth_port}"',
        f"proxy_prefix = {_quoted(inst.policy.auth_path)}",
        "reverse_proxy = true",
        f'upstreams = ["http://127.0.0.1:{inst.loop_port}"]',
        "pass_host_header = true",
        "pass_user_headers = true",
        f"pass_authorization_header = {'true' if wants_token else 'false'}",
        "set_xauthrequest = false",
        "skip_provider_button = true",
        "silence_ping_logging = true",
        "auth_logging = true",
        "request_logging = false",
    ]
    return "\n".join(lines) + "\n"


def auth_secret_refs(snapshot: Snapshot, se_group_id: UUID) -> list[str]:
    """The secret references the group's access sets need, sorted."""
    refs: set[str] = set()
    for inst in auth_instances(snapshot, se_group_id):
        refs |= {inst.policy.oidc.client_secret_ref, inst.policy.oidc.cookie_secret_ref}
    return sorted(refs)


def render_auth_files(
    snapshot: Snapshot,
    se_group_id: UUID,
    secrets: Mapping[str, str],
    broker_url: str | None = None,
) -> dict[str, str]:
    """The ``auth/<service>-<set>.cfg`` and ``.env`` files of an SE group (none without sets).

    Raises:
        RenderError: If a secret is missing or unusable, or a directory login has no broker URL.
    """
    view = snapshot.for_se_group(se_group_id)
    files: dict[str, str] = {}
    for inst in auth_instances(view, se_group_id):
        files[inst.path] = render_instance(inst, broker_url)
        files[inst.env_path] = render_env(inst, secrets)
    return files

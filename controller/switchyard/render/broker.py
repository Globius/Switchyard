# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render the login broker: one Dex instance per directory policy set (ADR 0137).

A *directory* (LDAP, SAML or local users) is not an OIDC provider, so a central broker presents it
as one. The broker runs next to the controller, not on the engines: engines stay stateless and the
engines' oauth2-proxy talks to it as to any OIDC issuer. One instance per directory keeps tenants
apart (Dex lets any client of an instance use any connector of it), so each instance has exactly
one connector and only the clients of the access sets that name it.

The functions are pure. Secrets (bind passwords, password hashes, client secrets) arrive resolved
from OpenBao and land only in the instance configs, which the broker host keeps owner-only.
"""

import base64
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import yaml

from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot
from switchyard.render import escape as esc
from switchyard.render.escape import RenderError

#: First loopback port of the broker instances.
BROKER_PORT_BASE = 5600
MAX_INSTANCES = 1000
_BCRYPT = re.compile(r"^\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}$")
_SECRET = re.compile(r"[A-Za-z0-9._~+/=-]{8,512}")
_NAMESPACE = uuid.UUID("5b3c6e9e-0f0a-4e5b-9a55-0c0d5a3f7a11")


@dataclass(frozen=True)
class Client:
    """An access set that signs its users in through the directory."""

    set_name: str
    client_id: str
    client_secret_ref: str
    redirect_uris: tuple[str, ...]
    lifetime_s: int


@dataclass(frozen=True)
class BrokerInstance:
    """One broker instance: a directory of one tenant and the clients that use it."""

    tenant: str
    directory: dm.DirectoryPolicySet
    clients: tuple[Client, ...]
    port: int

    @property
    def name(self) -> str:
        """File and path name, ``<tenant>/<directory>`` flattened with a dash for files."""
        return f"{esc.ident(self.tenant)}-{esc.ident(self.directory.name)}"

    def issuer(self, broker_url: str) -> str:
        """The issuer URL: the broker's public URL, the tenant and the directory."""
        return f"{broker_url.rstrip('/')}/{esc.ident(self.tenant)}/{esc.ident(self.directory.name)}"


def check_broker_url(value: str) -> str:
    """Return ``value`` if it is an ``https`` base URL without query or fragment, else raise.

    Example:
        >>> check_broker_url("https://auth.example.test/dex")
        'https://auth.example.test/dex'
        >>> check_broker_url("http://auth.example.test")
        Traceback (most recent call last):
            ...
        switchyard.render.escape.RenderError: the broker URL must be an https URL without query
    """
    parts = urlsplit(value)
    ok = (
        parts.scheme == "https"
        and parts.hostname
        and not parts.query
        and not parts.fragment
        and not parts.username
        and re.fullmatch(r"[A-Za-z0-9._~/:-]*", parts.path)
    )
    if not ok:
        raise RenderError("the broker URL must be an https URL without query")
    return value.rstrip("/")


def broker_instances(snapshot: Snapshot) -> list[BrokerInstance]:
    """The instances the snapshot needs: every directory some access set uses, sorted by name.

    Example:
        >>> broker_instances(Snapshot())
        []
    """
    tenants = {t.id: t.name for t in snapshot.tenants}
    directories = {p.id: p for p in snapshot.policy_sets if isinstance(p, dm.DirectoryPolicySet)}
    clients: dict[uuid.UUID, list[Client]] = {}
    for ps in snapshot.policy_sets:
        if isinstance(ps, dm.AccessPolicySet) and ps.oidc.directory_id in directories:
            oidc = ps.oidc
            uris = tuple(
                sorted(
                    f"https://{esc.hostname(h)}{esc.arg(ps.auth_path)}/callback"
                    for h in oidc.redirect_hosts
                )
            )
            client = Client(
                ps.name, oidc.client_id, oidc.client_secret_ref, uris, oidc.cookie_expire_s
            )
            clients.setdefault(oidc.directory_id, []).append(client)  # type: ignore[arg-type]
    ordered = sorted(
        clients, key=lambda d: (tenants[directories[d].tenant_id], directories[d].name)
    )
    if len(ordered) > MAX_INSTANCES:
        raise RenderError(f"more than {MAX_INSTANCES} directories in use")
    return [
        BrokerInstance(
            tenants[directories[d].tenant_id],
            directories[d],
            tuple(sorted(clients[d], key=lambda c: c.client_id)),
            BROKER_PORT_BASE + i,
        )
        for i, d in enumerate(ordered)
    ]


def broker_secret_refs(snapshot: Snapshot) -> list[str]:
    """Every secret reference the broker configs need, sorted."""
    refs: set[str] = set()
    for inst in broker_instances(snapshot):
        refs |= {c.client_secret_ref for c in inst.clients}
        conn = inst.directory.connector
        if isinstance(conn, dm.LdapConnector):
            refs.add(conn.bind_password_ref)
        elif isinstance(conn, dm.LocalConnector):
            refs |= {u.password_hash_ref for u in conn.users}
    return sorted(refs)


def _secret(secrets: Mapping[str, str], ref: str, what: str) -> str:
    if ref not in secrets:
        raise RenderError(f"missing secret {ref} for {what}")
    value = secrets[ref]
    if not _SECRET.fullmatch(value) and not _BCRYPT.fullmatch(value):
        raise RenderError(f"the secret for {what} is empty or has characters that are not allowed")
    return value


def _connector(inst: BrokerInstance, secrets: Mapping[str, str], issuer: str) -> dict[str, Any]:
    conn = inst.directory.connector
    name = esc.ident(inst.directory.name)
    if isinstance(conn, dm.LdapConnector):
        config: dict[str, Any] = {
            "host": f"{esc.hostname(conn.host)}:{conn.port}",
            "insecureNoSSL": False,
            "startTLS": conn.security == "starttls",
            "bindDN": conn.bind_dn,
            "bindPW": _secret(secrets, conn.bind_password_ref, f"directory {name}"),
            "usernamePrompt": "Username",
            "userSearch": {
                "baseDN": conn.user_base_dn,
                "filter": conn.user_filter,
                "username": conn.username_attr,
                "idAttr": conn.id_attr,
                "emailAttr": conn.email_attr,
                "nameAttr": conn.name_attr,
            },
        }
        if conn.root_ca_pem:
            config["rootCAData"] = base64.b64encode(conn.root_ca_pem.encode()).decode()
        if conn.group_base_dn:
            config["groupSearch"] = {
                "baseDN": conn.group_base_dn,
                "filter": conn.group_filter,
                "userMatchers": [
                    {"userAttr": conn.group_user_attr, "groupAttr": conn.group_member_attr}
                ],
                "nameAttr": conn.group_name_attr,
            }
        return {"type": "ldap", "id": "ldap", "name": name, "config": config}
    if isinstance(conn, dm.SamlConnector):
        config = {
            "ssoURL": conn.sso_url,
            "caData": base64.b64encode(conn.ca_pem.encode()).decode(),
            "redirectURI": f"{issuer}/callback",
            "usernameAttr": conn.username_attr,
            "emailAttr": conn.email_attr,
        }
        if conn.groups_attr:
            config["groupsAttr"] = conn.groups_attr
        return {"type": "saml", "id": "saml", "name": name, "config": config}
    raise RenderError("local users have no connector")  # pragma: no cover - handled by caller


def render_dex_config(inst: BrokerInstance, secrets: Mapping[str, str], broker_url: str) -> str:
    """The Dex configuration (YAML) of one instance.

    Raises:
        RenderError: If a secret is missing or unusable, or the broker URL is not acceptable.

    Example:
        >>> import uuid as _u
        >>> from switchyard.domain.models import DirectoryPolicySet, LocalConnector, LocalUser
        >>> d = DirectoryPolicySet(id=_u.UUID(int=1), tenant_id=_u.UUID(int=2), name="staff",
        ...     connector=LocalConnector(users=[LocalUser(username="a", email="a@example.test",
        ...         password_hash_ref="kv/tenants/t/a")]))
        >>> inst = BrokerInstance("t", d, (Client("s", "web", "kv/tenants/t/c",
        ...     ("https://x.example.test/oauth2/callback",), 3600),), 5600)
        >>> hashed = "$2b$10$" + "a" * 53
        >>> secrets = {"kv/tenants/t/a": hashed, "kv/tenants/t/c": "secret-value"}
        >>> text = render_dex_config(inst, secrets, "https://auth.example.test/dex")
        >>> "issuer: https://auth.example.test/dex/t/staff" in text
        True
    """
    base = check_broker_url(broker_url)
    issuer = inst.issuer(base)
    lifetime = max((c.lifetime_s for c in inst.clients), default=3600)
    config: dict[str, Any] = {
        "issuer": issuer,
        "storage": {
            "type": "sqlite3",
            "config": {"file": f"/var/lib/dex/{inst.name}.db"},
        },
        "web": {"http": f"127.0.0.1:{inst.port}"},
        "oauth2": {"skipApprovalScreen": True, "responseTypes": ["code"]},
        "expiry": {
            "idTokens": "10m",
            "refreshTokens": {
                "validForIfNotUsed": f"{lifetime}s",
                "absoluteLifetime": f"{lifetime}s",
            },
        },
        "staticClients": [
            {
                "id": c.client_id,
                "name": c.set_name,
                "secret": _secret(secrets, c.client_secret_ref, f"access set {c.set_name}"),
                "redirectURIs": list(c.redirect_uris),
            }
            for c in inst.clients
        ],
    }
    conn = inst.directory.connector
    if isinstance(conn, dm.LocalConnector):
        config["enablePasswordDB"] = True
        config["staticPasswords"] = [
            {
                "email": u.email,
                "username": u.username,
                "userID": str(uuid.uuid5(_NAMESPACE, f"{inst.name}/{u.username}")),
                "hash": _secret(secrets, u.password_hash_ref, f"user {u.username}"),
            }
            for u in sorted(conn.users, key=lambda u: u.username)
        ]
        for u in conn.users:
            if not _BCRYPT.fullmatch(secrets[u.password_hash_ref]):
                raise RenderError(f"the password hash of {u.username} is not a bcrypt hash")
    else:
        config["connectors"] = [_connector(inst, secrets, issuer)]
    header = "# Managed by Switchyard. Do not edit: changes are reported as drift.\n"
    return header + yaml.safe_dump(config, sort_keys=True, default_flow_style=False)


def render_broker_files(
    snapshot: Snapshot, secrets: Mapping[str, str], broker_url: str
) -> dict[str, str]:
    """All broker files: ``dex/<tenant>-<directory>.yaml``, ``instances.json`` and ``routes.conf``.

    ``instances.json`` lists the instances (name, public path, port) for the supervisor.
    ``routes.conf`` is an nginx snippet for the controller's public proxy: one ``location`` per
    instance that passes the request, path unchanged, to the instance on the loopback.
    """
    base = check_broker_url(broker_url)
    prefix = urlsplit(base).path.rstrip("/")
    files: dict[str, str] = {}
    listing: list[dict[str, object]] = []
    routes: list[str] = []
    for inst in broker_instances(snapshot):
        files[f"dex/{inst.name}.yaml"] = render_dex_config(inst, secrets, base)
        path = f"{prefix}/{esc.ident(inst.tenant)}/{esc.ident(inst.directory.name)}"
        listing.append({"name": inst.name, "path": path, "port": inst.port})
        routes.append(
            f"location {esc.arg(path)}/ {{\n"
            f"    proxy_pass http://127.0.0.1:{inst.port};\n"
            "    proxy_set_header Host $host;\n"
            "    proxy_set_header X-Forwarded-Proto $scheme;\n"
            "    proxy_set_header X-Forwarded-For $remote_addr;\n"
            "}\n"
        )
    if files:
        files["dex/instances.json"] = json.dumps({"instances": listing}, indent=2) + "\n"
        files["dex/routes.conf"] = (
            "# Managed by Switchyard. Included by the controller's public nginx (ADR 0137).\n"
            + "\n".join(routes)
        )
    return dict(sorted(files.items()))

"""``syctl``: the Switchyard command-line client.

In milestone M1 the commands that touch state talk to PostgreSQL directly (set
``SWITCHYARD_DATABASE_URL`` or pass ``--database-url``). From M2 on, ``syctl`` becomes a thin client
of the HTTP API and these direct-database commands remain only for bootstrap and recovery.

Exit codes: 0 success, 1 not found / already exists, 2 invalid state or unrenderable value.
"""

import asyncio
import contextlib
import os
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer

from switchyard import __version__
from switchyard.config import DEFAULT_DATABASE_URL
from switchyard.db import migrate
from switchyard.db.tenancy import ADMIN_ROLE, APP_ROLE, make_engine
from switchyard.domain.errors import DomainValidationError
from switchyard.render.escape import RenderError
from switchyard.services.adopt import adopt
from switchyard.services.errors import AlreadyExistsError, NotFoundError
from switchyard.services.render import render_se_group
from switchyard.services.seed import seed_example

app = typer.Typer(help="Switchyard command-line client.", no_args_is_help=True)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
app.add_typer(db_app, name="db")

DatabaseUrl = Annotated[
    str,
    typer.Option(
        "--database-url",
        envvar="SWITCHYARD_DATABASE_URL",
        help="SQLAlchemy URL (postgresql+psycopg://...). The user must be able to assume the "
        "switchyard_admin and switchyard_app roles.",
        show_default=False,
    ),
]


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine; on Windows use the selector loop, which psycopg requires."""
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)


def fail(message: str, code: int) -> typer.Exit:
    """Print ``message`` to stderr and return the exit to raise."""
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code)


@app.callback()
def main() -> None:
    """Switchyard command-line client."""


@app.command()
def version() -> None:
    """Print the Switchyard version."""
    typer.echo(__version__)


@db_app.command("upgrade")
def db_upgrade(
    url: DatabaseUrl = DEFAULT_DATABASE_URL,
    revision: Annotated[str, typer.Option(help="Target revision.")] = "head",
) -> None:
    """Create or upgrade the database schema (Alembic migrations)."""
    migrate.upgrade(url, revision)
    typer.echo(f"database is at revision {revision}")


@app.command()
def seed(
    example: Annotated[
        bool, typer.Option("--example", help="Load the demo tenant (made-up data).")
    ] = False,
    url: DatabaseUrl = DEFAULT_DATABASE_URL,
) -> None:
    """Load sample data for local testing. Currently only ``--example`` is supported."""
    if not example:
        raise fail("nothing to seed; pass --example", 2)

    async def go() -> str:
        engine = make_engine(url, role=ADMIN_ROLE)
        try:
            snapshot = await seed_example(engine)
        finally:
            await engine.dispose()
        return (
            f"seeded tenant {snapshot.tenants[0].name!r} with {len(snapshot.virtual_services)} "
            f"virtual services; try: syctl render --tenant {snapshot.tenants[0].name} "
            f"--se-group {snapshot.se_groups[0].name}"
        )

    try:
        typer.echo(run(go()))
    except AlreadyExistsError as exc:
        raise fail(str(exc), 1) from exc


@app.command()
def render(
    tenant: Annotated[str, typer.Option(help="Tenant name.")],
    se_group: Annotated[str, typer.Option("--se-group", help="SE group name.")],
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Write the files under this directory instead of printing them.",
            file_okay=False,
        ),
    ] = None,
    url: DatabaseUrl = DEFAULT_DATABASE_URL,
) -> None:
    """Render an SE group's HAProxy files from the database.

    Without ``--out`` every file is printed to stdout, each under a ``==> path <==`` header. With
    ``--out`` the files are written under that directory, ready to copy to
    ``/etc/switchyard/haproxy/``. The content hash is always printed to stderr.
    """

    async def go() -> Any:
        admin, app_engine = make_engine(url, role=ADMIN_ROLE), make_engine(url, role=APP_ROLE)
        try:
            return await render_se_group(admin, app_engine, tenant, se_group)
        finally:
            await admin.dispose()
            await app_engine.dispose()

    try:
        result = run(go())
    except NotFoundError as exc:
        raise fail(str(exc), 1) from exc
    except DomainValidationError as exc:
        raise fail(str(exc), 2) from exc
    except RenderError as exc:
        raise fail(str(exc), 2) from exc

    for warning in result.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)
    if out is None:
        for path, content in result.files.items():
            typer.echo(f"==> {path} <==")
            typer.echo(content, nl=False)
    else:
        root = out.resolve()
        for path, content in result.files.items():
            target = (root / path).resolve()
            if root not in target.parents:
                raise fail(f"refusing to write outside {root}: {path}", 2)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        typer.echo(f"wrote {len(result.files)} files to {root}", err=True)
    typer.echo(f"content hash {result.content_hash}", err=True)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8000,
) -> None:
    """Run the HTTP API (uvicorn)."""
    import uvicorn

    from switchyard import runtime
    from switchyard.api.app import build_services, create_app

    runtime.setup_audit_syslog()
    runtime.configure_secret_policy_from_env()
    db = runtime.databases()
    secrets = runtime.secrets_backend()
    services = build_services(
        db,
        runtime.directory(db),
        secrets,
        runtime.certificates(db, secrets),
        runtime.enrollment(db, secrets),
        runtime.sessions(db, secrets),
        runtime.telemetry(),
        proxmox=runtime.proxmox_client(),
        grafana=runtime.grafana(db),
        alerts=runtime.alert_service(db, secrets),
        email_configured=runtime.smtp_settings() is not None,
    )
    public_url = os.environ.get("SWITCHYARD_PUBLIC_URL")
    app = create_app(
        services,
        public_url=public_url,
        metrics_token=os.environ.get("SWITCHYARD_METRICS_TOKEN"),
        grafana_url=runtime.grafana_url(),
    )
    config = uvicorn.Config(app, host=host, port=port)
    # uvicorn's own loop is Proactor on Windows, which psycopg rejects; run() uses the selector.
    run(uvicorn.Server(config).serve())


@app.command("adopt")
def adopt_command(
    file: Annotated[Path, typer.Argument(help="Adoption file (snapshot YAML plus `engines`).")],
    url: DatabaseUrl = DEFAULT_DATABASE_URL,
) -> None:
    """Adopt infrastructure built outside the controller, such as the management tenant."""

    async def go() -> str:
        engine = make_engine(url, role=ADMIN_ROLE)
        try:
            snapshot, engines = await adopt(engine, file)
        finally:
            await engine.dispose()
        return (
            f"adopted tenant {snapshot.tenants[0].name!r}: {len(snapshot.se_groups)} SE groups, "
            f"{len(snapshot.virtual_services)} virtual services, {engines} engines"
        )

    try:
        typer.echo(run(go()))
    except AlreadyExistsError as exc:
        raise fail(str(exc), 1) from exc
    except (ValueError, DomainValidationError, OSError) as exc:
        raise fail(str(exc), 2) from exc


@app.command("openbao-policy")
def openbao_policy(
    shared: Annotated[
        list[str] | None, typer.Option("--shared", help="A shared prefix such as kv/se/.")
    ] = None,
    platform: Annotated[
        list[str] | None,
        typer.Option("--platform", help="A platform secret such as kv/proxmox/token."),
    ] = None,
    mount: Annotated[str, typer.Option(help="KV v2 mount.")] = "kv",
) -> None:
    """Print the OpenBao policy for the controller's token (matches the secret path rules)."""
    from switchyard.services.openbao_policy import controller_policy

    typer.echo(controller_policy(shared or [], mount=mount, platform=platform or []), nl=False)


@app.command("worker")
def run_worker(
    name: Annotated[str, typer.Option(help="Worker id recorded on claimed jobs.")] = "worker-1",
) -> None:
    """Run a job worker until interrupted."""
    from switchyard import runtime

    runtime.setup_audit_syslog()
    runtime.configure_secret_policy_from_env()

    async def go() -> None:
        db = runtime.databases()
        stop = asyncio.Event()
        worker, scheduler = runtime.worker(db, name)
        try:
            await asyncio.gather(worker.run_forever(stop), scheduler.run_forever(stop))
        finally:
            await db.dispose()

    with contextlib.suppress(KeyboardInterrupt):
        run(go())


@app.command("setting")
def set_setting(
    key: Annotated[str, typer.Argument(help="Setting name, for example allow_lxc_guests.")],
    value: Annotated[bool, typer.Argument(help="true or false.")],
    url: DatabaseUrl = DEFAULT_DATABASE_URL,
) -> None:
    """Set a controller-wide setting directly in the database (labs and bootstrap)."""
    from switchyard import runtime
    from switchyard.services.context import Databases
    from switchyard.services.settings import SystemSettings

    async def go() -> None:
        db = (
            runtime.databases()
            if url == DEFAULT_DATABASE_URL
            else Databases(
                app=make_engine(url, role=APP_ROLE), admin=make_engine(url, role=ADMIN_ROLE)
            )
        )
        try:
            await SystemSettings(db).set_value(key, value)
        finally:
            await db.dispose()

    try:
        run(go())
    except ValueError as exc:
        raise fail(str(exc), 2) from exc
    typer.echo(f"{key} = {str(value).lower()}")


controller_app = typer.Typer(
    help="The controller's own address, with database access only (recovery).",
    no_args_is_help=True,
)
app.add_typer(controller_app, name="controller")


def _controller_run(url: str, action: str) -> Any:
    from switchyard import runtime
    from switchyard.services.changesets import ChangeSetService
    from switchyard.services.context import Databases
    from switchyard.services.controller_settings import ControllerSettings

    async def go() -> Any:
        db = (
            runtime.databases()
            if url == DEFAULT_DATABASE_URL
            else Databases(
                app=make_engine(url, role=APP_ROLE), admin=make_engine(url, role=ADMIN_ROLE)
            )
        )
        try:
            service = ControllerSettings(db, ChangeSetService(db))
            if action == "revert":
                return await service.reset()
            return await service.status_text()
        finally:
            await db.dispose()

    return run(go())


@controller_app.command("status")
def controller_status(url: DatabaseUrl = DEFAULT_DATABASE_URL) -> None:
    """Show the public URL and any change that waits for confirmation."""
    typer.echo(_controller_run(url, "status"))


@controller_app.command("revert")
def controller_revert(url: DatabaseUrl = DEFAULT_DATABASE_URL) -> None:
    """Revert a change that waits for confirmation, without the API (the worker applies it)."""
    done = _controller_run(url, "revert")
    typer.echo("reverted" if done else "nothing waits for confirmation")


@app.command("token")
def create_system_token(
    name: Annotated[str, typer.Option(help="Token name.")] = "bootstrap",
    url: DatabaseUrl = DEFAULT_DATABASE_URL,
) -> None:
    """Create a system-admin API token directly in the database (bootstrap and recovery)."""
    from switchyard import runtime
    from switchyard.domain.workflow import Role
    from switchyard.services.context import Databases, Principal
    from switchyard.services.tokens import TokenService

    async def go() -> str:
        db = (
            runtime.databases()
            if url == DEFAULT_DATABASE_URL
            else Databases(
                app=make_engine(url, role=APP_ROLE), admin=make_engine(url, role=ADMIN_ROLE)
            )
        )
        try:
            root = Principal(actor="cli:bootstrap", role=Role.SYSTEM_ADMIN)
            plaintext, _ = await TokenService(db).create(root, None, name, Role.SYSTEM_ADMIN)
            return plaintext
        finally:
            await db.dispose()

    typer.echo(run(go()))


broker_app = typer.Typer(
    help="The login broker for LDAP, SAML and local users.", no_args_is_help=True
)
app.add_typer(broker_app, name="broker")


@broker_app.command("render")
def broker_render(
    out: Annotated[Path, typer.Option("--out", help="Directory for dex/*.yaml.", file_okay=False)],
    broker_url: Annotated[
        str | None,
        typer.Option(
            "--broker-url",
            envvar="SWITCHYARD_AUTH_BROKER_URL",
            help="Public base URL of the broker, for example https://auth.example.test/dex.",
        ),
    ] = None,
    url: DatabaseUrl = DEFAULT_DATABASE_URL,
) -> None:
    """Write one Dex config per directory in use (ADR 0137); print the files that changed."""
    from switchyard import runtime
    from switchyard.services.broker import BrokerError, build_broker_files, write_broker_files

    if not broker_url:
        raise fail("set --broker-url or SWITCHYARD_AUTH_BROKER_URL", 2)

    async def go() -> dict[str, str]:
        admin = make_engine(url, role=ADMIN_ROLE)
        try:
            return await build_broker_files(admin, runtime.secrets_backend(), broker_url)
        finally:
            await admin.dispose()

    try:
        files = run(go())
    except BrokerError as exc:
        raise fail(str(exc), 2) from exc
    for rel in write_broker_files(files, out):
        typer.echo(rel)


jwks_app = typer.Typer(help="Work with JSON Web Key Sets (JWT policy sets).", no_args_is_help=True)
app.add_typer(jwks_app, name="jwks")


@jwks_app.command("convert")
def jwks_convert(
    file: Annotated[Path, typer.Argument(help="A JWKS document (JSON).")],
) -> None:
    """Print the ``keys`` of a ``jwt`` policy set made from a JWKS, as JSON."""
    import json

    from switchyard.services.jwks import JwksError, jwks_to_keys

    try:
        keys = jwks_to_keys(file.read_text("utf-8"))
    except JwksError as exc:
        raise fail(str(exc), 2) from exc
    typer.echo(json.dumps(keys, indent=2))


idp_app = typer.Typer(help="Identity providers for user login (OIDC).", no_args_is_help=True)
app.add_typer(idp_app, name="idp")


def _databases(url: str) -> Any:
    from switchyard import runtime
    from switchyard.services.context import Databases

    if url == DEFAULT_DATABASE_URL:
        return runtime.databases()
    return Databases(app=make_engine(url, role=APP_ROLE), admin=make_engine(url, role=ADMIN_ROLE))


@idp_app.command("add")
def idp_add(
    name: Annotated[str, typer.Argument(help="Short name shown on the login page.")],
    issuer: Annotated[str, typer.Option(help="OIDC issuer URL (https://...).")],
    client_id: Annotated[str, typer.Option(help="OAuth client id.")],
    client_secret_ref: Annotated[
        str | None, typer.Option(help="Secret reference of the client secret.")
    ] = None,
    groups_claim: Annotated[str, typer.Option(help="ID-token claim with the groups.")] = "groups",
    username_claim: Annotated[str, typer.Option(help="Claim shown as the user name.")] = (
        "preferred_username"
    ),
    url: DatabaseUrl = DEFAULT_DATABASE_URL,
) -> None:
    """Add an OIDC identity provider."""
    from switchyard.services.idp import IdentityProviderAdmin

    async def go() -> None:
        db = _databases(url)
        try:
            await IdentityProviderAdmin(db).add_provider(
                name,
                issuer,
                client_id,
                client_secret_ref=client_secret_ref,
                groups_claim=groups_claim,
                username_claim=username_claim,
            )
        finally:
            await db.dispose()

    run(go())
    typer.echo(f"added identity provider {name}")


@idp_app.command("add-saml")
def idp_add_saml(
    name: Annotated[str, typer.Argument(help="Short name shown on the login page.")],
    idp_entity_id: Annotated[str, typer.Option(help="The IdP's entity id (assertion issuer).")],
    sso_url: Annotated[str, typer.Option(help="The IdP's single sign-on URL (https://...).")],
    cert_file: Annotated[
        list[Path], typer.Option(help="PEM file with a certificate that signs assertions.")
    ],
    sp_entity_id: Annotated[
        str | None, typer.Option(help="Our entity id (default derived).")
    ] = None,
    groups_attribute: Annotated[str, typer.Option(help="Attribute with the groups.")] = "groups",
    username_attribute: Annotated[str, typer.Option(help="Attribute with the name.")] = "username",
    email_attribute: Annotated[str, typer.Option(help="Attribute with the e-mail.")] = "email",
    url: DatabaseUrl = DEFAULT_DATABASE_URL,
) -> None:
    """Add a SAML 2.0 identity provider (SP metadata: /api/v1/auth/saml/<name>/metadata)."""
    from switchyard.services.idp import IdentityProviderAdmin

    certificates = [path.read_text(encoding="utf-8") for path in cert_file]

    async def go() -> None:
        db = _databases(url)
        try:
            await IdentityProviderAdmin(db).add_saml_provider(
                name,
                idp_entity_id=idp_entity_id,
                sso_url=sso_url,
                signing_certificates=certificates,
                sp_entity_id=sp_entity_id,
                attribute_groups=groups_attribute,
                attribute_username=username_attribute,
                attribute_email=email_attribute,
            )
        finally:
            await db.dispose()

    run(go())
    typer.echo(f"added SAML identity provider {name}")


@idp_app.command("map")
def idp_map(
    provider: Annotated[str, typer.Argument(help="Provider name.")],
    group: Annotated[str, typer.Argument(help="Group name in the provider's groups claim.")],
    role: Annotated[str, typer.Argument(help="viewer, operator, tenant-admin or system-admin.")],
    tenant: Annotated[str | None, typer.Option(help="Tenant (not for system-admin).")] = None,
    url: DatabaseUrl = DEFAULT_DATABASE_URL,
) -> None:
    """Grant a role to members of a provider group."""
    from switchyard.domain.workflow import Role
    from switchyard.services.idp import IdentityProviderAdmin

    async def go() -> None:
        db = _databases(url)
        try:
            await IdentityProviderAdmin(db).map_group(provider, group, Role(role), tenant=tenant)
        finally:
            await db.dispose()

    run(go())
    typer.echo(f"{group} -> {role}" + (f" in {tenant}" if tenant else ""))


@idp_app.command("list")
def idp_list(url: DatabaseUrl = DEFAULT_DATABASE_URL) -> None:
    """List providers and their group mappings."""
    from switchyard.services.idp import IdentityProviderAdmin

    async def go() -> list[Any]:
        db = _databases(url)
        try:
            return await IdentityProviderAdmin(db).list_providers()
        finally:
            await db.dispose()

    for p in run(go()):
        typer.echo(f"{p.name} {p.protocol} {'enabled' if p.enabled else 'DISABLED'} {p.issuer_url}")
        for group, tenant, role in p.mappings:
            typer.echo(f"  {group} -> {role.value}" + (f" in {tenant}" if tenant else ""))


@idp_app.command("enable")
def idp_enable(
    provider: str, url: DatabaseUrl = DEFAULT_DATABASE_URL, disable: bool = False
) -> None:
    """Enable a provider (or disable it with --disable)."""
    from switchyard.services.idp import IdentityProviderAdmin

    async def go() -> None:
        db = _databases(url)
        try:
            await IdentityProviderAdmin(db).set_enabled(provider, not disable)
        finally:
            await db.dispose()

    run(go())


@app.command("local-admin")
def local_admin(
    username: Annotated[str, typer.Argument(help="Break-glass user name.")] = "breakglass",
    url: DatabaseUrl = DEFAULT_DATABASE_URL,
) -> None:
    """Create the break-glass administrator (password + TOTP) and print the TOTP secret once.

    The password is read from the terminal. The TOTP secret is stored in the secrets backend under
    ``kv/switchyard/break-glass/<username>`` (needs OpenBao); login stays off until
    ``SWITCHYARD_BREAK_GLASS=1``.
    """
    import base64
    import secrets as pysecrets

    from switchyard import runtime
    from switchyard.adapters.secrets import OpenBaoSecrets
    from switchyard.services.oidc import OidcClient
    from switchyard.services.sessions import SessionService

    password = typer.prompt("Password (16+ characters)", hide_input=True, confirmation_prompt=True)
    store = runtime.secrets_backend()
    if not isinstance(store, OpenBaoSecrets):
        raise typer.BadParameter("storing the TOTP secret needs OpenBao to be configured")
    ref = f"kv/switchyard/break-glass/{username}"
    totp = base64.b32encode(pysecrets.token_bytes(20)).decode()

    async def go() -> None:
        db = _databases(url)
        try:
            await store.put(ref, totp)
            await SessionService(db, OidcClient(), store).create_local_admin(
                username, password, ref
            )
        finally:
            await db.dispose()

    run(go())
    typer.echo(f"TOTP secret (add it to an authenticator app now, it is not shown again): {totp}")


api_app = typer.Typer(
    help="Work with a running controller over its HTTP API.", no_args_is_help=True
)
app.add_typer(api_app, name="api")

#: Replaced in tests to talk to an in-process application instead of the network.
API_TRANSPORT: Any = None


def _api() -> Any:
    from switchyard.client import ApiClient, load_profile

    return ApiClient(load_profile(), transport=API_TRANSPORT)


def _api_run(coro: Coroutine[Any, Any, Any]) -> Any:
    from switchyard.client import ApiClientError

    try:
        return run(coro)
    except ApiClientError as exc:
        typer.echo(f"error: {exc}", err=True)
        for err in exc.errors:
            typer.echo(f"  {err.get('path')}: {err.get('message')}", err=True)
        raise typer.Exit(1) from exc


Tenant = Annotated[str, typer.Option("--tenant", "-t", help="Tenant name or id.")]


@api_app.command("login")
def api_login(
    url: Annotated[str, typer.Option(help="Controller base URL.")],
    token: Annotated[str, typer.Option(help="API token (stored in a file readable by you only).")],
) -> None:
    """Check the token against the controller and save it for later commands."""
    from switchyard.client import ApiClient, Profile, save_profile

    async def go() -> Any:
        client = ApiClient(Profile(url, token), transport=API_TRANSPORT)
        try:
            return await client.request("GET", "/me")
        finally:
            await client.close()

    me = _api_run(go())
    path = save_profile(Profile(url, token))
    typer.echo(f"logged in as {me['name']}; saved to {path}")


@api_app.command("whoami")
def api_whoami() -> None:
    """Show who the controller thinks you are and your role in each tenant."""

    async def go() -> Any:
        client = _api()
        try:
            return await client.request("GET", "/me")
        finally:
            await client.close()

    me = _api_run(go())
    typer.echo(f"{me['name']} ({'system administrator' if me['is_system'] else 'user'})")
    for t in me["tenants"]:
        typer.echo(f"  {t['name']}: {t['role']}")


@api_app.command("list")
def api_list(collection: str, tenant: Tenant) -> None:
    """List objects: virtual-services, pools, health-monitors, certificates, ..."""

    async def go() -> list[dict[str, Any]]:
        client = _api()
        try:
            items: list[dict[str, Any]] = []
            cursor: str | None = None
            while True:
                page = await client.request(
                    "GET",
                    f"/tenants/{tenant}/{collection}",
                    params={"cursor": cursor} if cursor else None,
                )
                items += page["items"]
                cursor = page.get("next_cursor")
                if not cursor:
                    return items
        finally:
            await client.close()

    for item in _api_run(go()):
        typer.echo(f"{item['name']}\t{item['id']}\tv{item['version']}")


@api_app.command("get")
def api_get(collection: str, ref: str, tenant: Tenant) -> None:
    """Print one object as JSON (``ref`` is its name or id)."""
    import json

    async def go() -> Any:
        client = _api()
        try:
            page = await client.request(
                "GET", f"/tenants/{tenant}/{collection}", params={"name": ref}
            )
            found = [o for o in page["items"] if o["name"] == ref or o["id"] == ref]
            if not found:
                page = await client.request("GET", f"/tenants/{tenant}/{collection}")
                found = [o for o in page["items"] if o["id"] == ref]
            return found[0] if found else None
        finally:
            await client.close()

    obj = _api_run(go())
    if obj is None:
        typer.echo(f"error: no {collection} named {ref}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(obj, indent=2, sort_keys=True))


@api_app.command("apply")
def api_apply(
    collection: str,
    file: Annotated[Path, typer.Option("--file", "-f", help="JSON file with the object body.")],
    tenant: Tenant,
    wait: Annotated[bool, typer.Option(help="Wait for the change to be rolled out.")] = True,
) -> None:
    """Create an object from a JSON file and roll it out (one change set)."""
    import json

    body = json.loads(file.read_text())

    async def go() -> Any:
        client = _api()
        try:
            accepted = await client.request(
                "POST", f"/tenants/{tenant}/{collection}", json_body=body, params={"apply": "true"}
            )
            if wait:
                await client.wait_job(accepted["job_id"])
            return accepted
        finally:
            await client.close()

    accepted = _api_run(go())
    typer.echo(
        f"{accepted['object_id']} change set {accepted['change_set_id']} job {accepted['job_id']}"
    )


@api_app.command("delete")
def api_delete(
    collection: str,
    object_id: str,
    tenant: Tenant,
    wait: Annotated[bool, typer.Option(help="Wait for the change to be rolled out.")] = True,
) -> None:
    """Delete an object by id and roll the change out."""

    async def go() -> Any:
        client = _api()
        try:
            page = await client.request("GET", f"/tenants/{tenant}/{collection}")
            current = next((o for o in page["items"] if o["id"] == object_id), None)
            if current is None:
                raise typer.BadParameter(f"no {collection} with id {object_id}")
            accepted = await client.request(
                "DELETE",
                f"/tenants/{tenant}/{collection}/{object_id}",
                params={"apply": "true"},
                headers={"If-Match": f'"{current["version"]}"'},
            )
            if wait:
                await client.wait_job(accepted["job_id"])
            return accepted
        finally:
            await client.close()

    accepted = _api_run(go())
    typer.echo(f"deleted; change set {accepted['change_set_id']} job {accepted['job_id']}")


@api_app.command("status")
def api_status(tenant: Tenant) -> None:
    """Show each engine: reachable or not, and whether it has drifted from the desired state."""

    async def go() -> Any:
        client = _api()
        try:
            return await client.request("GET", f"/tenants/{tenant}/status/engines")
        finally:
            await client.close()

    for e in _api_run(go()):
        state = "up" if e["reachable"] else f"UNREACHABLE ({e['error']})"
        drift = f", DRIFT: {', '.join(d['kind'] for d in e['drift'])}" if e["drift"] else ""
        typer.echo(f"{e['engine']}\t{state}{drift}")


@api_app.command("job")
def api_job(job_id: str, wait: bool = False) -> None:
    """Show a job; with --wait, block until it finishes."""
    import json

    async def go() -> Any:
        client = _api()
        try:
            if wait:
                return await client.wait_job(job_id)
            return await client.request("GET", f"/jobs/{job_id}")
        finally:
            await client.close()

    typer.echo(json.dumps(_api_run(go()), indent=2, sort_keys=True))


# -- ACME providers, DNS credentials and certificate requests (ADR 0080) -----------------------

acme_app = typer.Typer(help="ACME providers (system administrators).", no_args_is_help=True)
dns_app = typer.Typer(help="A tenant's DNS credentials for DNS-01.", no_args_is_help=True)
cert_app = typer.Typer(help="Request and check certificates.", no_args_is_help=True)
api_app.add_typer(acme_app, name="acme")
api_app.add_typer(dns_app, name="dns")
api_app.add_typer(cert_app, name="cert")


def _pairs(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"{item!r} is not key=value")
        out[key] = value
    return out


async def _call(
    method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
) -> Any:
    client = _api()
    try:
        return await client.request(method, path, json_body=body, headers=headers)
    finally:
        await client.close()


async def _find(path: str, name: str) -> dict[str, Any]:
    for item in await _call("GET", path):
        if item["name"] == name or item["id"] == name:
            return item  # type: ignore[no-any-return]
    raise typer.BadParameter(f"nothing named {name} under {path}")


def _etag(item: dict[str, Any]) -> dict[str, str]:
    return {"If-Match": f'"{item["version"]}"'}


@acme_app.command("presets")
def acme_presets() -> None:
    """List the known certificate authorities."""
    for p in _api_run(_call("GET", "/acme-provider-presets")):
        where = p["directory_url"] or "(enter it)"
        typer.echo(f"{p['key']}\t{p['label']}\t{p['auth_mode']}\t{where}")


@acme_app.command("list")
def acme_list() -> None:
    """List the ACME providers."""
    for p in _api_run(_call("GET", "/acme-providers")):
        flags = ("default " if p["default"] else "") + (
            "registered" if p["account_registered"] else "untested"
        )
        typer.echo(
            f"{p['name']}\t{p['preset']}\t{p['directory_url']}\t{flags}\tused by {p['used_by']}"
        )


@acme_app.command("add")
def acme_add(
    name: str,
    preset: Annotated[str, typer.Option(help="letsencrypt, zerossl, google, keyfactor ...")] = (
        "custom"
    ),
    directory_url: Annotated[
        str | None, typer.Option(help="Needed for custom and keyfactor.")
    ] = None,
    email: Annotated[str | None, typer.Option(help="Contact address.")] = None,
    eab_kid: Annotated[str | None, typer.Option(help="External Account Binding key id.")] = None,
    eab_hmac_key: Annotated[str | None, typer.Option(help="EAB HMAC key (base64url).")] = None,
    default: Annotated[bool, typer.Option(help="Use it when a certificate names none.")] = False,
    terms_agreed: Annotated[bool, typer.Option(help="Accept the CA's terms of service.")] = False,
) -> None:
    """Register an ACME provider; the controller creates its account key."""
    body = {
        "name": name,
        "preset": preset,
        "directory_url": directory_url,
        "email": email,
        "eab_kid": eab_kid,
        "eab_hmac_key": eab_hmac_key,
        "default": default,
        "terms_agreed": terms_agreed,
    }
    made = _api_run(
        _call("POST", "/acme-providers", {k: v for k, v in body.items() if v is not None})
    )
    typer.echo(f"{made['name']}\t{made['id']}\tv{made['version']}")


@acme_app.command("test")
def acme_test(name: str) -> None:
    """Contact the CA and register the account; prints each step."""

    async def go() -> Any:
        found = await _find("/acme-providers", name)
        return await _call("POST", f"/acme-providers/{found['id']}/test")

    result = _api_run(go())
    for stage in result["stages"]:
        typer.echo(f"{stage['name']}\t{'ok' if stage['ok'] else 'FAILED'}\t{stage['detail']}")
    if not result["ok"]:
        raise typer.Exit(1)


@acme_app.command("delete")
def acme_delete(name: str) -> None:
    """Remove an ACME provider that no certificate uses."""

    async def go() -> None:
        found = await _find("/acme-providers", name)
        await _call("DELETE", f"/acme-providers/{found['id']}", headers=_etag(found))

    _api_run(go())


@dns_app.command("types")
def dns_types() -> None:
    """List the provider types with their fields (secret fields are marked)."""
    for t in _api_run(_call("GET", "/dns-provider-types")):
        typer.echo(f"{t['type']}\t{t['label']}")
        for f in t["fields"]:
            kind = "secret" if f["secret"] else "setting"
            typer.echo(f"  {f['key']}\t{kind}\t{'required' if f['required'] else 'optional'}")


@dns_app.command("list")
def dns_list(tenant: Tenant) -> None:
    """List the tenant's DNS credentials."""
    for p in _api_run(_call("GET", f"/tenants/{tenant}/dns-providers")):
        zones = ",".join(p["zones"]) or "(any)"
        typer.echo(f"{p['name']}\t{p['type']}\t{zones}\t{'on' if p['enabled'] else 'off'}")


@dns_app.command("add")
def dns_add(
    name: str,
    tenant: Tenant,
    type_: Annotated[str, typer.Option("--type", help="cloudflare, route53, azure, google ...")],
    zone: Annotated[list[str] | None, typer.Option(help="A zone this credential serves.")] = None,
    config: Annotated[list[str] | None, typer.Option(help="key=value setting (repeat).")] = None,
    secret: Annotated[list[str] | None, typer.Option(help="key=value credential (repeat).")] = None,
) -> None:
    """Add a DNS credential. Secrets go to the secrets backend and are never shown again."""
    body = {
        "name": name,
        "type": type_,
        "zones": zone or [],
        "config": _pairs(config),
        "credentials": _pairs(secret),
    }
    made = _api_run(_call("POST", f"/tenants/{tenant}/dns-providers", body))
    typer.echo(f"{made['name']}\t{made['id']}\tv{made['version']}")


@dns_app.command("test")
def dns_test(name: str, tenant: Tenant) -> None:
    """Publish and remove a TXT record in each zone of the credential."""

    async def go() -> Any:
        found = await _find(f"/tenants/{tenant}/dns-providers", name)
        return await _call("POST", f"/tenants/{tenant}/dns-providers/{found['id']}/test", {})

    result = _api_run(go())
    for z in result["zones"]:
        typer.echo(f"{z['zone']}\t{'ok' if z['ok'] else 'FAILED'}\t{z['detail']}")
    if not result["ok"]:
        raise typer.Exit(1)


@dns_app.command("delete")
def dns_delete(name: str, tenant: Tenant) -> None:
    """Remove a DNS credential that no certificate names."""

    async def go() -> None:
        found = await _find(f"/tenants/{tenant}/dns-providers", name)
        await _call(
            "DELETE", f"/tenants/{tenant}/dns-providers/{found['id']}", headers=_etag(found)
        )

    _api_run(go())


def _issue_body(
    name: str,
    san: list[str],
    acme_provider: str | None,
    challenge: str | None,
    dns_provider: str | None,
    renew_before_days: int | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "source": "acme", "sans": san}
    for key, value in (
        ("acme_provider", acme_provider),
        ("challenge", challenge),
        ("dns_provider", dns_provider),
        ("renew_before_days", renew_before_days),
    ):
        if value is not None:
            body[key] = value
    return body


@api_app.command("import-avi")
def api_import_avi(
    file: Annotated[Path, typer.Argument(help="Avi configuration export (JSON).")],
    tenant: Tenant,
    se_group: Annotated[str, typer.Option(help="SE group the services will use.")],
    vip_pool: Annotated[str | None, typer.Option(help="VIP pool for the new VIPs.")] = None,
    vip: Annotated[
        list[str] | None, typer.Option(help="OLD=NEW fixed address (Avi VIP name or address).")
    ] = None,
    prefix: Annotated[str, typer.Option(help="Put in front of every new name.")] = "",
    include_unused: Annotated[bool, typer.Option(help="Also convert unused objects.")] = False,
    only: Annotated[list[str] | None, typer.Option(help="Convert only this service.")] = None,
    commit: Annotated[
        bool, typer.Option(help="Create the draft change set (default: report only).")
    ] = False,
) -> None:
    """Convert an Avi export; report what would be created, or open a draft change set."""
    import json

    try:
        export = json.loads(file.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise fail(f"cannot read {file}: {exc}", 2) from exc
    body = {
        "export": export,
        "se_group": se_group,
        "vip_pool": vip_pool,
        "vip_map": _pairs(vip),
        "prefix": prefix,
        "include_unused": include_unused,
        "only": only or [],
        "dry_run": not commit,
    }
    result = _api_run(_call("POST", f"/tenants/{tenant}/imports/avi", body))
    typer.echo(
        ("created a draft change set" if commit else "dry run")
        + ": "
        + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in result["counts"].items())
    )
    for v in result["vips"]:
        typer.echo(f"VIP {v['avi_vsvip']}: {v['address']} -> {v['new_address'] or '(none)'}")
    for f in result["findings"]:
        if f["level"] != "info":
            typer.echo(f"{f['level']:8} {f['object']}: {f['message']}")
    if result["change_set_id"]:
        typer.echo(f"change set {result['change_set_id']}")


@api_app.command("controller-reset")
def api_controller_reset(
    confirm: Annotated[bool, typer.Option(help="Confirm at once (from this address).")] = False,
) -> None:
    """Propose dropping the controller's configured address (the environment default applies)."""
    result = _api_run(_call("PUT", "/system/controller/pending", {"reset_to_default": True}))
    typer.echo(f"waiting for confirmation until {result['pending']['expires_at']}")
    if confirm:
        _api_run(_call("POST", "/system/controller/confirm", None))
        typer.echo("confirmed")


@api_app.command("k8s-sync")
def api_k8s_sync(cluster: str, tenant: Tenant) -> None:
    """Queue a sync of a Kubernetes cluster (name or id) and print the job."""
    found = _api_run(_call("GET", f"/tenants/{tenant}/kubernetes-clusters"))
    match = next((c for c in found if cluster in (c["name"], c["id"])), None)
    if match is None:
        raise fail(f"no Kubernetes cluster {cluster}", 2)
    job = _api_run(_call("POST", f"/tenants/{tenant}/kubernetes-clusters/{match['id']}/sync", None))
    typer.echo(f"job {job['job_id']}")


@cert_app.command("check")
def cert_check(
    name: str,
    tenant: Tenant,
    san: Annotated[list[str], typer.Option(help="A name for the certificate (repeat).")],
    acme_provider: Annotated[str | None, typer.Option()] = None,
    challenge: Annotated[str | None, typer.Option(help="dns-01 or http-01.")] = None,
    dns_provider: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Check an ACME certificate request without queueing it."""
    body = _issue_body(name, san, acme_provider, challenge, dns_provider, None)
    result = _api_run(_call("POST", f"/tenants/{tenant}/certificates/issue/check", body))
    for problem in result["problems"]:
        typer.echo(f"{problem['path']}: {problem['message']}")
    if not result["ok"]:
        raise typer.Exit(1)
    typer.echo("ok")


@cert_app.command("issue")
def cert_issue(
    name: str,
    tenant: Tenant,
    san: Annotated[list[str], typer.Option(help="A name for the certificate (repeat).")],
    acme_provider: Annotated[str | None, typer.Option()] = None,
    challenge: Annotated[str | None, typer.Option(help="dns-01 or http-01.")] = None,
    dns_provider: Annotated[str | None, typer.Option()] = None,
    renew_before_days: Annotated[int | None, typer.Option()] = None,
    wait: Annotated[bool, typer.Option(help="Wait for the job to finish.")] = True,
) -> None:
    """Request an ACME certificate (a job; it ends in a change set)."""
    import json

    async def go() -> Any:
        client = _api()
        try:
            job = await client.request(
                "POST",
                f"/tenants/{tenant}/certificates/issue",
                json_body=_issue_body(
                    name, san, acme_provider, challenge, dns_provider, renew_before_days
                ),
            )
            return await client.wait_job(job["job_id"]) if wait else job
        finally:
            await client.close()

    typer.echo(json.dumps(_api_run(go()), indent=2, sort_keys=True))


@cert_app.command("renewals")
def cert_renewals(tenant: Tenant) -> None:
    """Show when each certificate renews and how the last attempt went."""
    for s in _api_run(_call("GET", f"/tenants/{tenant}/certificate-renewals")):
        if s["last_ok"] is None:
            last = "never tried"
        else:
            last = "ok" if s["last_ok"] else f"FAILED: {s['last_error']}"
        typer.echo(f"{s['certificate']}\t{s['issuer']}\trenews {s['renew_due_at'][:10]}\t{last}")

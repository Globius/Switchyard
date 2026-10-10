"""``syctl``: the Switchyard command-line client.

In milestone M1 the commands that touch state talk to PostgreSQL directly (set
``SWITCHYARD_DATABASE_URL`` or pass ``--database-url``). From M2 on, ``syctl`` becomes a thin client
of the HTTP API and these direct-database commands remain only for bootstrap and recovery.

Exit codes: 0 success, 1 not found / already exists, 2 invalid state or unrenderable value.
"""

import asyncio
import contextlib
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

    db = runtime.databases()
    uvicorn.run(create_app(build_services(db, runtime.directory(db))), host=host, port=port)


@app.command("worker")
def run_worker(
    name: Annotated[str, typer.Option(help="Worker id recorded on claimed jobs.")] = "worker-1",
) -> None:
    """Run a job worker until interrupted."""
    from switchyard import runtime

    async def go() -> None:
        db = runtime.databases()
        stop = asyncio.Event()
        try:
            await runtime.worker(db, name).run_forever(stop)
        finally:
            await db.dispose()

    with contextlib.suppress(KeyboardInterrupt):
        run(go())


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

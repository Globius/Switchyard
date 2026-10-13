# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""`syctl release` and `syctl upgrade`: build and check upgrade bundles (ADR 0205).

``release`` works on files only (no controller needed): make a signing key, build a signed bundle,
verify one. ``upgrade`` talks to a running controller: the check, a backup, the feed and a plan.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from alembic.script import ScriptDirectory
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from switchyard import __version__
from switchyard.db.migrate import alembic_config
from switchyard.domain.bundle import BundleError, Kind
from switchyard.domain.releases import ReleaseNotes, public_key_b64
from switchyard.services.release_bundle import Input, build, verify
from switchyard.services.release_feed import PROJECT_KEYS

release_app = typer.Typer(help="Build and verify signed upgrade bundles.", no_args_is_help=True)
upgrade_app = typer.Typer(
    help="Check and prepare an upgrade of a running controller.", no_args_is_help=True
)


def _trusted(keys: list[str]) -> dict[str, str]:
    out = dict(PROJECT_KEYS)
    for item in keys:
        key_id, _, value = item.partition("=")
        if not value:
            raise typer.BadParameter("use --trust KEY_ID=BASE64_PUBLIC_KEY")
        out[key_id] = value
    return out


Trust = Annotated[
    list[str],
    typer.Option("--trust", help="A trusted public key, KEY_ID=BASE64. Repeat for several."),
]


@release_app.command("keygen")
def keygen(
    out: Annotated[Path, typer.Argument(help="Where to write the private key (PEM, mode 0600).")],
    key_id: Annotated[str, typer.Option(help="Name of the key in feeds and bundles.")] = "release",
) -> None:
    """Create an Ed25519 signing key. Keep the private key offline; publish the public one."""
    if out.exists():
        typer.echo(f"error: {out} already exists", err=True)
        raise typer.Exit(1)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    typer.echo(f"private key written to {out}")
    typer.echo(f"trust it with:  --trust {key_id}={public_key_b64(key)}")


@release_app.command("build")
def build_cmd(
    out: Annotated[
        Path, typer.Argument(help="The bundle to write, for example switchyard-0.2.0.sybundle.")
    ],
    key: Annotated[
        Path | None, typer.Option(help="Signing key from `syctl release keygen`.")
    ] = None,
    unsigned: Annotated[
        bool,
        typer.Option("--unsigned", help="Write a bundle without a signature (development only)."),
    ] = False,
    key_id: Annotated[str, typer.Option(help="The key's id.")] = "release",
    version: Annotated[str, typer.Option(help="Release version.")] = __version__,
    controller_image: Annotated[list[Path], typer.Option(help="`docker save` tarball.")] = [],  # noqa: B006
    proxy_image: Annotated[list[Path], typer.Option(help="`docker save` tarball.")] = [],  # noqa: B006
    helm_chart: Annotated[list[Path], typer.Option(help="Packaged chart (.tgz).")] = [],  # noqa: B006
    compose: Annotated[list[Path], typer.Option(help="Compose file or env example.")] = [],  # noqa: B006
    agent: Annotated[list[Path], typer.Option(help="sy-agent binary.")] = [],  # noqa: B006
    se_template: Annotated[list[Path], typer.Option(help="SE template image or archive.")] = [],  # noqa: B006
    upgrade_from: Annotated[
        list[str], typer.Option(help="A version this can be installed over.")
    ] = [],  # noqa: B006
    min_agent: Annotated[
        str, typer.Option(help="Oldest agent this controller pushes to.")
    ] = "0.0.0",
    recommended_min_agent: Annotated[
        str, typer.Option(help="Oldest fully supported agent.")
    ] = "0.0.0",
    notes: Annotated[
        Path | None, typer.Option(help="JSON file: new, deprecated, breaking, action_required.")
    ] = None,
) -> None:
    """Pack files into a signed bundle (tar: manifest, signature, files with SHA-256)."""
    groups: list[tuple[Kind, list[Path]]] = [
        ("controller-image", controller_image),
        ("proxy-image", proxy_image),
        ("helm-chart", helm_chart),
        ("compose", compose),
        ("agent", agent),
        ("se-template", se_template),
    ]
    inputs = [Input(kind, p, p.name) for kind, paths in groups for p in paths]
    if not inputs:
        typer.echo("error: give at least one file to include", err=True)
        raise typer.Exit(1)
    head = ScriptDirectory.from_config(alembic_config("postgresql+psycopg://x")).get_current_head()
    if (key is None) == (not unsigned):
        typer.echo("error: give --key (signed) or --unsigned, not both or neither", err=True)
        raise typer.Exit(1)
    try:
        private = (
            None
            if key is None
            else serialization.load_pem_private_key(key.read_bytes(), password=None)
        )
        if private is not None and not isinstance(private, Ed25519PrivateKey):
            raise BundleError("the key is not an Ed25519 key")
        manifest = build(
            out,
            version=version,
            schema_head=head or "",
            created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            inputs=inputs,
            key=private,
            key_id=key_id,
            upgrade_from=upgrade_from,
            min_agent=min_agent,
            recommended_min_agent=recommended_min_agent,
            notes=ReleaseNotes.model_validate_json(notes.read_text()) if notes else None,
        )
    except (BundleError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"{out}: {manifest.version}, schema {manifest.schema_head}, {len(manifest.files)} files"
    )


@release_app.command("feed")
def feed_cmd(
    out: Annotated[Path, typer.Argument(help="Where to write releases.json.")],
    key: Annotated[Path, typer.Option(help="The signing key from `syctl release keygen`.")],
    bundle: Annotated[
        list[str], typer.Option(help="PATH=URL of a bundle and where it will be published.")
    ],
    key_id: Annotated[str, typer.Option(help="The key's id.")] = "release",
    channel: Annotated[str, typer.Option(help="stable, candidate or nightly.")] = "stable",
    notes_url: Annotated[str | None, typer.Option(help="Release notes page (https).")] = None,
    keep: Annotated[
        int | None, typer.Option(help="Keep only the newest N releases of this channel.")
    ] = None,
    previous: Annotated[
        Path | None, typer.Option(help="An earlier releases.json to add to (must verify).")
    ] = None,
) -> None:
    """Write the signed releases.json that controllers read, from published bundles."""
    from switchyard.domain.releases import FeedError
    from switchyard.release_feed_build import build_feed, load_previous

    if channel not in ("stable", "candidate", "nightly"):
        typer.echo("error: channel is stable, candidate or nightly", err=True)
        raise typer.Exit(1)
    pairs: list[tuple[Path, str]] = []
    for item in bundle:
        path, _, url = item.partition("=")
        if not url.startswith("https://"):
            typer.echo(f"error: {item!r} needs PATH=https://URL", err=True)
            raise typer.Exit(1)
        pairs.append((Path(path), url))
    try:
        private = serialization.load_pem_private_key(key.read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise BundleError("the key is not an Ed25519 key")
        doc = build_feed(
            private,
            key_id,
            pairs,
            channel=channel,  # type: ignore[arg-type]
            notes_url=notes_url,
            keep=keep,
            previous=load_previous(previous) if previous else None,
        )
    except (BundleError, FeedError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    versions = [r["version"] for r in doc["payload"]["releases"]]
    typer.echo(f"{out}: {len(versions)} releases ({', '.join(versions)}), signed with {key_id}")


@release_app.command("verify")
def verify_cmd(bundle: Path, trust: Trust = []) -> None:  # noqa: B006
    """Check the signature, every file's SHA-256 and the member list. Extracts nothing."""
    try:
        manifest = verify(bundle, _trusted(trust))
    except BundleError as exc:
        typer.echo(f"REFUSED: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"ok: switchyard {manifest.version}, schema {manifest.schema_head}")
    for f in manifest.files:
        typer.echo(f"  {f.kind:<17} {f.size_bytes:>12}  {f.sha256[:12]}  {f.path}")
    if manifest.upgrade_from:
        typer.echo(f"installs over: {', '.join(manifest.upgrade_from)}")
    for group in ("breaking", "action_required", "deprecated", "new"):
        for line in getattr(manifest.notes, group):
            typer.echo(f"  [{group}] {line}")


def _call(method: str, path: str, body: Any = None) -> Any:
    from switchyard import cli

    async def go() -> Any:
        client = cli._api()
        try:
            return await client.request(method, path, json_body=body)
        finally:
            await client.close()

    return cli._api_run(go())


@upgrade_app.command("check")
def check() -> None:
    """Versions of the controller, database and engines, with advice. Changes nothing."""
    result = _call("GET", "/system/upgrade/check")
    typer.echo(
        f"controller {result['controller_version']} (latest known {result['latest_version']}), "
        f"database {result['schema_current']} -> {result['schema_head']} [{result['schema_state']}]"
    )
    for f in result["findings"]:
        typer.echo(f"[{f['severity']}] {f['subject']}: {f['message']}")
        if f["action"]:
            typer.echo(f"    -> {f['action']}")
    if not result["findings"]:
        typer.echo("nothing to do")
    if result.get("update"):
        u = result["update"]
        typer.echo(f"update: {u['version']} ({u['date']}), install order: {' -> '.join(u['path'])}")


@upgrade_app.command("backup")
def backup(
    target: Annotated[str, typer.Argument(help="Name of a backup target.")],
    reason: Annotated[str, typer.Option(help="manual, pre-upgrade or scheduled.")] = "pre-upgrade",
) -> None:
    """Write a configuration backup now (run it before an upgrade)."""
    run = _call("POST", "/system/backups", {"target": target, "reason": reason})
    typer.echo(f"{run['archive']}  {run['size_bytes']} bytes  sha256 {run['sha256'][:16]}")


@upgrade_app.command("plan")
def plan(
    bundle: Path,
    trust: Trust = [],  # noqa: B006
    allow_unsigned: Annotated[
        bool, typer.Option("--allow-unsigned", help="Plan for a bundle that has no signature.")
    ] = False,
) -> None:
    """Dry run: verify a bundle and say what applying it would involve. Changes nothing."""
    try:
        manifest = verify(bundle, _trusted(trust), allow_unsigned=allow_unsigned)
    except BundleError as exc:
        typer.echo(f"REFUSED: {exc}", err=True)
        raise typer.Exit(1) from exc
    result = _call("GET", "/system/upgrade/check")
    typer.echo(f"bundle: switchyard {manifest.version} (schema {manifest.schema_head})")
    typer.echo(f"running: {result['controller_version']} (schema {result['schema_current']})")
    blockers = [f for f in result["findings"] if f["severity"] == "error"]
    warns = [f for f in result["findings"] if f["severity"] == "warning"]
    if manifest.upgrade_from and result["controller_version"] not in manifest.upgrade_from:
        typer.echo(
            f"warning: this bundle installs over {', '.join(manifest.upgrade_from)}; "
            f"you run {result['controller_version']}. Go through the intermediate releases."
        )
    steps = [
        "1. back up the configuration (syctl upgrade backup TARGET) and the database (pgBackRest)",
        "2. load the controller and proxy images on each controller node",
        "3. run the schema migration once (syctl db upgrade), additive by rule (ADR 0205)",
        "4. restart controller nodes one at a time, checking /api/v1/healthz after each",
        "5. roll engines to the new template one at a time, gated on the group health score",
    ]
    typer.echo("steps:")
    for line in steps:
        typer.echo(f"  {line}")
    for f in blockers + warns:
        typer.echo(f"[{f['severity']}] {f['subject']}: {f['message']}")
    if blockers:
        typer.echo("errors above should be resolved first")
        raise typer.Exit(2)


#: Replaced in tests; real use runs commands with subprocess.
APPLY_RUNNER: Any = None


@upgrade_app.command("apply")
def apply_cmd(
    bundle: Path,
    compose_file: Annotated[
        list[Path],
        typer.Option(
            help="This node's Compose file; repeat it for an override file (compose.override.yaml)."
        ),
    ],
    env_file: Annotated[
        Path, typer.Option(help="This node's env file (CONTROLLER_IMAGE, PROXY_IMAGE).")
    ],
    health_url: Annotated[
        str, typer.Option(help="This node's API, for the health check.")
    ] = "http://127.0.0.1:8000",
    workdir: Annotated[Path, typer.Option(help="Where to unpack the bundle (must be new).")] = Path(
        "sy-upgrade"
    ),
    backup_target: Annotated[str | None, typer.Option(help="Back up to this target first.")] = None,
    no_backup: Annotated[
        bool, typer.Option(help="Upgrade without a configuration backup.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
    allow_unsigned: Annotated[
        bool,
        typer.Option(
            "--allow-unsigned",
            help="Install a bundle without a verified signature. File hashes are still checked.",
        ),
    ] = False,
    confirm_unsigned: Annotated[
        str | None,
        typer.Option(help="With --allow-unsigned: type the bundle version to confirm it."),
    ] = None,
    trust: Trust = [],  # noqa: B006
) -> None:
    """Upgrade THIS Docker Compose controller node. Run it on one node at a time.

    Verifies the bundle, takes a configuration backup (or `--no-backup`), loads the images,
    runs the migration, recreates api, worker and proxy, and waits until the node is healthy. If it
    does not become healthy the node goes back to its old images.
    """
    import asyncio

    import httpx

    from switchyard.services.upgrade_apply import ApplyError, SubprocessRunner, apply_compose

    if backup_target is None and not no_backup:
        typer.echo("error: give --backup-target NAME, or --no-backup to go without", err=True)
        raise typer.Exit(1)
    try:
        manifest = verify(bundle, _trusted(trust), allow_unsigned=allow_unsigned)
    except BundleError as exc:
        typer.echo(f"REFUSED: {exc}", err=True)
        raise typer.Exit(1) from exc
    if allow_unsigned:
        typer.echo(
            "WARNING: this bundle is NOT verified by a signature; only its file hashes were "
            "checked, which proves the files match the manifest and nothing about who made them.",
            err=True,
        )
        if confirm_unsigned != manifest.version:
            typer.echo(
                f"error: to install it anyway, add --confirm-unsigned {manifest.version}", err=True
            )
            raise typer.Exit(1)
    typer.echo(f"upgrading this node to switchyard {manifest.version}")
    for line in manifest.notes.breaking + manifest.notes.action_required:
        typer.echo(f"  note: {line}")
    if not yes and not typer.confirm("continue?"):
        raise typer.Exit(1)
    if backup_target is not None:
        run_ = _call("POST", "/system/backups", {"target": backup_target, "reason": "pre-upgrade"})
        typer.echo(f"backup written: {run_['archive']}")

    async def healthy() -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                return (
                    await client.get(f"{health_url.rstrip('/')}/api/v1/healthz")
                ).status_code == 200
        except httpx.HTTPError:
            return False

    try:
        _, report = asyncio.run(
            apply_compose(
                bundle,
                _trusted(trust),
                compose_file=compose_file,
                env_file=env_file,
                workdir=workdir,
                healthy=healthy,
                run=APPLY_RUNNER or SubprocessRunner(),
                allow_unsigned=allow_unsigned,
            )
        )
    except (ApplyError, BundleError) as exc:
        typer.echo(f"FAILED: {exc}", err=True)
        raise typer.Exit(3 if isinstance(exc, ApplyError) and not exc.rolled_back else 1) from exc
    for step in report.steps:
        typer.echo(f"  ok: {step}")
    typer.echo("done. Next node, then the engines (runbooks/rolling-upgrade.md).")

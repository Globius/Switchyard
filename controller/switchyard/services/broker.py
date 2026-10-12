"""Write the login broker's files (ADR 0137): one Dex config per directory in use.

The broker host runs ``syctl broker render`` in a loop (or once per deployment step); a supervisor
there starts one Dex per ``dex/*.yaml`` and restarts the ones whose file changed. This module reads
the desired state of every tenant, resolves the secrets from OpenBao and writes the files, leaving
files that did not change untouched and removing the ones for directories that are gone.
"""

import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.adapters.secrets import SecretsReader
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session
from switchyard.render.broker import broker_secret_refs, render_broker_files
from switchyard.render.escape import RenderError


class BrokerError(Exception):
    """The broker files cannot be built (a secret is missing or a value is unusable)."""


async def build_broker_files(
    admin: AsyncEngine, secrets: SecretsReader, broker_url: str
) -> dict[str, str]:
    """Render the broker files for every directory in use, across all tenants.

    Raises:
        BrokerError: If a secret is missing or a value cannot be rendered safely.
    """
    async with admin_session(admin) as session:
        snapshot = await load_snapshot(session)
    found: dict[str, str] = {}
    for ref in broker_secret_refs(snapshot):
        try:
            found[ref] = await secrets.get(ref)
        except LookupError as exc:
            raise BrokerError(f"secret {ref} not found") from exc
    try:
        return render_broker_files(snapshot, found, broker_url)
    except RenderError as exc:
        raise BrokerError(str(exc)) from exc


def write_broker_files(files: dict[str, str], out: Path) -> list[str]:
    """Write ``files`` under ``out`` (owner-only), drop stale ``dex/`` files, return the changes.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     write_broker_files({"dex/a.yaml": "x"}, Path(d))
        ...     write_broker_files({"dex/a.yaml": "x"}, Path(d))
        ['dex/a.yaml']
        []
    """
    changed: list[str] = []
    for rel, content in files.items():
        target = out / rel
        if target.exists() and target.read_text("utf-8") == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        tmp.replace(target)
        changed.append(rel)
    directory = out / "dex"
    if directory.is_dir():
        for stale in sorted(directory.iterdir()):
            rel = f"dex/{stale.name}"
            if stale.is_file() and not stale.name.startswith(".") and rel not in files:
                stale.unlink()
                changed.append(rel)
    return changed

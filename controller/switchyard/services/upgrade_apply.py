# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Apply an upgrade bundle to one Docker Compose controller node (ADR 0205).

Run it on each node in turn; that is what makes the rollout one node at a time. Steps: verify the
bundle, load its images, point the node's env file at them, run the migration once, recreate
`api`, `worker` and `proxy`, and wait for ``/api/v1/healthz``. If the node does not become healthy,
the env file gets its previous image names back, the services are recreated on the old images,
and the failure is reported: the old and new version are never both half-running on one node.

Commands go through :class:`Runner`, so tests use a fake; real use is
:class:`SubprocessRunner` (argument lists, never a shell). Not yet exercised on a real node
(ADR 0205, "Not proven").
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from switchyard.domain.bundle import BundleError, Manifest
from switchyard.services.release_bundle import unpack

_LOADED = re.compile(r"^Loaded image: (\S+)$", re.MULTILINE)
_IMAGE_VARS = {"controller-image": "CONTROLLER_IMAGE", "proxy-image": "PROXY_IMAGE"}
_ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


class ApplyError(Exception):
    """The node could not be upgraded. ``rolled_back`` says whether it runs the old images again."""

    def __init__(self, message: str, *, rolled_back: bool = False) -> None:
        """Create the error."""
        super().__init__(message)
        self.rolled_back = rolled_back


class Runner(Protocol):
    """Runs a command (an argument list) and returns its exit code and combined output."""

    async def __call__(self, argv: list[str]) -> tuple[int, str]:
        """Run ``argv``."""
        ...


class SubprocessRunner:
    """Runs commands with ``asyncio.create_subprocess_exec``; no shell is involved."""

    async def __call__(self, argv: list[str]) -> tuple[int, str]:
        """Run ``argv``."""
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace")


@dataclass
class ApplyReport:
    """What was done, in order, for the operator and the audit trail."""

    version: str
    steps: list[str] = field(default_factory=list)
    images: dict[str, str] = field(default_factory=dict)


def read_env(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines (comments and blanks are ignored)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _ENV_LINE.match(line.strip())
        if m:
            out[m[1]] = m[2]
    return out


def set_env(text: str, updates: dict[str, str]) -> str:
    """Return ``text`` with the given variables set, keeping every other line as it was.

    Example:
        >>> nl = chr(10)
        >>> text = nl.join(["A=1", "# note", "B=2"])
        >>> set_env(text, {"B": "9", "C": "3"}).splitlines()
        ['A=1', '# note', 'B=9', 'C=3']
    """
    remaining = dict(updates)
    lines = []
    for line in text.splitlines():
        m = _ENV_LINE.match(line.strip())
        if m and m[1] in remaining:
            lines.append(f"{m[1]}={remaining.pop(m[1])}")
        else:
            lines.append(line)
    lines += [f"{k}={v}" for k, v in remaining.items()]
    return "\n".join(lines) + "\n"


async def apply_compose(
    bundle: Path,
    trusted: dict[str, str],
    *,
    compose_file: Path,
    env_file: Path,
    workdir: Path,
    healthy: Callable[[], Awaitable[bool]],
    run: Runner,
    health_attempts: int = 60,
    health_delay_s: float = 2.0,
    allow_unsigned: bool = False,
) -> tuple[Manifest, ApplyReport]:
    """Upgrade this node to the bundle's release.

    Args:
        bundle: The ``.sybundle`` file.
        trusted: Trusted public keys (id to base64).
        compose_file: The node's Compose file.
        env_file: The node's env file (holds ``CONTROLLER_IMAGE`` and ``PROXY_IMAGE``).
        workdir: A directory to unpack the bundle into (must not hold an earlier unpack).
        healthy: Returns whether this node's API answers ``/api/v1/healthz``.
        run: Command runner.
        health_attempts: How many times to ask ``healthy`` before giving up.
        health_delay_s: Seconds between attempts.
        allow_unsigned: Accept a bundle without a verified signature (hashes are still checked).

    Raises:
        BundleError: The bundle does not verify; nothing was touched.
        ApplyError: A step failed; ``rolled_back`` tells whether the old images run again.
    """
    manifest = unpack(bundle, workdir, trusted, allow_unsigned=allow_unsigned)
    report = ApplyReport(manifest.version)
    report.steps.append("bundle verified and unpacked")
    previous_text = env_file.read_text() if env_file.exists() else ""

    new_images: dict[str, str] = {}
    for entry in manifest.files:
        var = _IMAGE_VARS.get(entry.kind)
        if var is None:
            continue
        code, out = await run(["docker", "load", "-i", str(workdir / entry.path)])
        loaded = _LOADED.findall(out)
        if code != 0 or len(loaded) != 1:
            raise ApplyError(f"docker could not load {entry.path.rsplit('/', 1)[-1]}")
        new_images[var] = loaded[0]
        report.steps.append(f"loaded {loaded[0]}")
    if not new_images:
        raise ApplyError("the bundle holds no controller or proxy image")
    report.images = dict(new_images)

    base = ["docker", "compose", "-f", str(compose_file), "--env-file", str(env_file)]
    services = ["api", "worker", "proxy"]

    async def recreate() -> bool:
        code, _ = await run([*base, "up", "-d", "--no-deps", "--force-recreate", *services])
        return code == 0

    async def wait_healthy() -> bool:
        for _ in range(health_attempts):
            if await healthy():
                return True
            await asyncio.sleep(health_delay_s)
        return False

    env_file.write_text(set_env(previous_text, new_images))
    report.steps.append("env file points at the new images")

    async def revert(reason: str) -> ApplyError:
        env_file.write_text(previous_text)
        restored = await recreate() and await wait_healthy()
        report.steps.append("reverted to the previous images" if restored else "revert failed")
        suffix = "; the previous images are running again" if restored else "; REVERT FAILED"
        return ApplyError(reason + suffix, rolled_back=restored)

    code, out = await run([*base, "run", "--rm", "--no-deps", "migrate"])
    if code != 0:
        # The migration is additive by rule, so the old code still works on whatever it did.
        env_file.write_text(previous_text)
        raise ApplyError("the schema migration failed; the node still runs the old images")
    report.steps.append("schema migrated")
    if not await recreate():
        raise await revert("could not recreate the services")
    report.steps.append("services recreated")
    if not await wait_healthy():
        raise await revert("the node did not become healthy")
    report.steps.append("node healthy")
    return manifest, report


__all__ = [
    "ApplyError",
    "ApplyReport",
    "BundleError",
    "Runner",
    "SubprocessRunner",
    "apply_compose",
]

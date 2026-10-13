# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Validate rendered files with the real ``haproxy -c`` in a throwaway container.

The backend design's "Validate" stage runs in a sandbox with the same HAProxy version as the SE
image. Files are copied into the container with the Docker API (no bind mounts), so it behaves the
same on Windows, WSL2, Linux and Docker-in-Docker. The container has no network and is removed
afterwards. Certificates never reach the sandbox: throwaway self-signed ones are generated for
every path a crt-list names.
"""

import asyncio
import io
import re
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import docker

from switchyard.adapters.haproxy import ConfigRejected

HAPROXY_IMAGE = "haproxy:3.2"

_SCRIPT = r"""
set -eu
mkdir -p /etc/switchyard/certs /run/haproxy
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout /tmp/key.pem -out /tmp/crt.pem -subj /CN=validation -days 1 2>/dev/null
cat /tmp/crt.pem /tmp/key.pem > /tmp/bundle.pem
for list in /etc/switchyard/certs/*.crtlist; do
  [ -e "$list" ] || continue
  while read -r path _; do cp /tmp/bundle.pem "$path"; done < "$list"
done
haproxy -c -f /etc/switchyard/haproxy/haproxy.cfg
"""


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of ``haproxy -c``.

    Attributes:
        ok: True if HAProxy accepted the configuration.
        output: HAProxy's output.
    """

    ok: bool
    output: str

    @property
    def warnings(self) -> list[str]:
        """Lines HAProxy reported as ``[WARNING]``."""
        return [line for line in self.output.splitlines() if "[WARNING]" in line]


class SandboxValidator(Protocol):
    """Checks rendered files before any engine is touched."""

    async def check(self, files: Mapping[str, str]) -> SandboxResult:
        """Run the check and return its result (it does not raise on a rejected config)."""
        ...


def _tar(files: Mapping[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, content in sorted(files.items()):
            data = content.encode()
            info = tarfile.TarInfo(f"etc/switchyard/{path}")
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _run(files: Mapping[str, str], image: str) -> SandboxResult:
    client = docker.from_env()
    container = client.containers.create(
        image, command=["sh", "-c", _SCRIPT], user="root", network_mode="none"
    )
    try:
        container.put_archive("/", _tar(files))
        container.start()
        status = container.wait()["StatusCode"]
        output = container.logs().decode(errors="replace")
    finally:
        container.remove(force=True)
    return SandboxResult(ok=status == 0, output=re.sub(r"\(\d+\)", "", output))


class DockerSandbox:
    """:class:`SandboxValidator` that runs ``haproxy -c`` in a Docker container."""

    def __init__(self, image: str = HAPROXY_IMAGE) -> None:
        """Create the validator for an image (use the SE image's HAProxy version)."""
        self._image = image

    async def check(self, files: Mapping[str, str]) -> SandboxResult:
        """Validate ``files`` in a fresh container."""
        return await asyncio.to_thread(_run, files, self._image)


async def require_valid(sandbox: SandboxValidator, files: Mapping[str, str]) -> SandboxResult:
    """Run the sandbox and raise :class:`ConfigRejected` unless the configuration is valid."""
    result = await sandbox.check(files)
    if not result.ok:
        raise ConfigRejected(f"sandbox haproxy -c rejected the configuration: {result.output}")
    return result

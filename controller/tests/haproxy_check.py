"""Validate rendered files with the real ``haproxy -c`` inside the ``haproxy:3.2`` image.

Files are copied into the container with the Docker API (not bind mounts), so this works the same
on Windows, WSL2, Linux and a CI job that talks to a Docker-in-Docker daemon.
"""

import io
import re
import tarfile
import time
from collections.abc import Mapping
from dataclasses import dataclass

import docker

HAPROXY_IMAGE = "haproxy:3.2"

# Creates a throwaway self-signed PEM for every certificate a crt-list names, because the real
# certificates live in OpenBao and never reach the validation sandbox.
_SCRIPT = r"""
set -eu
mkdir -p /etc/switchyard/certs /run/haproxy
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout /tmp/key.pem -out /tmp/crt.pem -subj /CN=validation -days 1 2>/dev/null
cat /tmp/crt.pem /tmp/key.pem > /tmp/bundle.pem
for list in /etc/switchyard/haproxy/crt-lists/*; do
  [ -e "$list" ] || continue
  while read -r path _; do cp /tmp/bundle.pem "$path"; done < "$list"
done
haproxy -c -f /etc/switchyard/haproxy/haproxy.cfg
"""


@dataclass(frozen=True)
class CheckResult:
    """Outcome of ``haproxy -c``."""

    ok: bool
    output: str

    @property
    def warnings(self) -> list[str]:
        """Lines HAProxy reported as ``[WARNING]``."""
        return [line for line in self.output.splitlines() if "[WARNING]" in line]


def _tar(files: Mapping[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, content in sorted(files.items()):
            data = content.encode()
            info = tarfile.TarInfo(f"etc/switchyard/haproxy/{path}")
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def haproxy_check(files: Mapping[str, str]) -> CheckResult:
    """Run ``haproxy -c`` against rendered ``files`` and return the verdict and its output."""
    client = docker.from_env()
    container = client.containers.create(
        HAPROXY_IMAGE, command=["sh", "-c", _SCRIPT], user="root", network_mode="none"
    )
    try:
        container.put_archive("/", _tar(files))
        container.start()
        status = container.wait()["StatusCode"]
        output = container.logs().decode(errors="replace")
    finally:
        container.remove(force=True)
    return CheckResult(ok=status == 0, output=_clean(output))


def _clean(output: str) -> str:
    return re.sub(r"\(\d+\)", "", output)


def wait_for_docker(timeout_s: float = 30) -> None:
    """Block until the Docker daemon answers, or raise."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            docker.from_env().ping()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)

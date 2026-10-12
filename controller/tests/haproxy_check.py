"""Validate rendered files with the real ``haproxy -c`` inside the ``haproxy:3.2`` image.

Files are copied into the container with the Docker API (not bind mounts), so this works the same
on Windows, WSL2, Linux and a CI job that talks to a Docker-in-Docker daemon.
"""

import atexit
import contextlib
import io
import re
import tarfile
import time
from collections.abc import Mapping
from dataclasses import dataclass

import docker
from docker.models.containers import Container

HAPROXY_IMAGE = "haproxy:3.2"

# Creates a throwaway self-signed PEM for every certificate a crt-list names, because the real
# certificates live in OpenBao and never reach the validation sandbox.
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
# Client certificates HAProxy presents to pool members (``crt`` on a server line).
grep -oE ' crt /etc/switchyard/certs/[^ ]+[.]pem' /etc/switchyard/haproxy/haproxy.cfg | sort -u |
while read -r _ path; do cp /tmp/bundle.pem "$path"; done
# Client CAs and their revocation lists: a throwaway CA certificate and an empty CRL.
if grep -qE 'ca-file|crl-file' /etc/switchyard/haproxy/haproxy.cfg; then
  mkdir -p /tmp/ca && touch /tmp/ca/index.txt && echo 01 > /tmp/ca/crlnumber
  printf '[ca]
default_ca=x
[x]
database=/tmp/ca/index.txt
crlnumber=/tmp/ca/crlnumber
'     > /tmp/ca/ca.cnf
  printf 'default_md=sha256
default_crl_days=1
private_key=/tmp/key.pem
certificate=/tmp/crt.pem
'     >> /tmp/ca/ca.cnf
  openssl ca -gencrl -config /tmp/ca/ca.cnf -out /tmp/crl.pem 2>/dev/null
  grep -oE '(ca-file|crl-file) [^ ]+' /etc/switchyard/haproxy/haproxy.cfg | sort -u |
  while read -r kind path; do
    if [ "$kind" = ca-file ]; then cp /tmp/crt.pem "$path"; else cp /tmp/crl.pem "$path"; fi
  done
fi
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
        return [
            line
            for line in self.output.splitlines()
            if "[WARNING]" in line and "can't connect socket for nameserver" not in line
        ]


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


_WIPE = "rm -rf /etc/switchyard /run/haproxy /tmp/ca /tmp/*.pem"
_shared: dict[str | None, Container] = {}


def _container(hostname: str | None) -> Container:
    """A long-lived ``haproxy`` container per host name, reused by every check of this process.

    Creating a container per check was most of the test suite's run time on a Docker-in-Docker runner;
    each check now wipes the files of the previous one and runs the same script with ``exec``.
    """
    container = _shared.get(hostname)
    if container is None:
        container = docker.from_env().containers.run(
            HAPROXY_IMAGE,
            command=["sleep", "infinity"],
            user="root",
            network_mode="none",
            hostname=hostname,
            detach=True,
        )
        _shared[hostname] = container
    return container


@atexit.register
def _remove_shared() -> None:
    for container in _shared.values():
        with contextlib.suppress(Exception):
            container.remove(force=True)
    _shared.clear()


def haproxy_check(files: Mapping[str, str]) -> CheckResult:
    """Run ``haproxy -c`` against rendered ``files`` and return the verdict and its output."""
    # A ``peers`` section only stays when one of its peers carries the host name of the engine.
    local = re.search(r"^\s+peer (\S+) ", files.get("haproxy/haproxy.cfg", ""), re.MULTILINE)
    container = _container(local.group(1) if local else None)
    container.exec_run(["sh", "-c", _WIPE], user="root")
    container.put_archive("/", _tar(files))
    status, output = container.exec_run(["sh", "-c", _SCRIPT], user="root")
    return CheckResult(ok=status == 0, output=_clean(output.decode(errors="replace")))


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

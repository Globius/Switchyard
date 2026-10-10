"""A throwaway Docker lab: real HAProxy + Data Plane API engines, backends and clients.

Everything lives on one bridge network in the RFC 5737 range 192.0.2.0/24:

- Service engines run ``haproxytech/haproxy-debian:3.2`` (HAProxy master-worker plus the Data
  Plane API as a second process), with their API published on a random loopback port.
- Backends are tiny HAProxy instances that answer with what they received.
- Clients are one-shot ``curl`` containers.
"""

import io
import tarfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import docker
import httpx

SE_IMAGE = "haproxytech/haproxy-debian:3.2"
BACKEND_IMAGE = "haproxy:3.2"
CURL_IMAGE = "curlimages/curl:8.10.1"
SUBNET = "192.0.2.0/24"
API_USER = "admin"
API_PASSWORD = "adminpwd"  # lab only

_DATAPLANE_YML = """\
config_version: 2
name: sy-lab
dataplaneapi:
  host: 0.0.0.0
  port: 5555
  user:
    - name: admin
      insecure: true
      password: adminpwd
  transaction:
    transaction_dir: /tmp/haproxy-transactions
  resources:
    maps_dir: /etc/switchyard/haproxy/maps
    ssl_certs_dir: /etc/switchyard/certs
    general_storage_dir: /etc/switchyard/haproxy/lists
    spoe_dir: /etc/haproxy/spoe
haproxy:
  config_file: /etc/switchyard/haproxy/haproxy.cfg
  haproxy_bin: /usr/local/sbin/haproxy
  reload:
    reload_delay: 1
    reload_cmd: /seed/reload.sh
    restart_cmd: /seed/reload.sh
    reload_strategy: custom
"""

_BOOTSTRAP_CFG = """\
global
    stats socket /run/haproxy/admin.sock mode 660 level admin expose-fd listeners
defaults
    mode http
    timeout connect 5s
    timeout client 5s
    timeout server 5s
"""

_RELOAD = '#!/bin/sh\nkill -USR2 "$(cat /run/haproxy.pid)"\n'

_START = """\
set -eu
mkdir -p /etc/switchyard/haproxy/maps /etc/switchyard/haproxy/lists /etc/switchyard/certs \\
  /etc/haproxy/spoe /tmp/haproxy-transactions /run/haproxy
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \\
  -keyout /tmp/k.pem -out /tmp/c.pem -subj /CN=lab -days 1 2>/dev/null
cat /tmp/c.pem /tmp/k.pem > /tmp/bundle.pem
for name in $CERTS; do cp /tmp/bundle.pem "/etc/switchyard/certs/$name.pem"; done
cp /seed/haproxy.cfg /etc/switchyard/haproxy/haproxy.cfg
cp /seed/dataplaneapi.yml /etc/haproxy/dataplaneapi.yml
haproxy -W -db -p /run/haproxy.pid -f /etc/switchyard/haproxy/haproxy.cfg &
sleep 1
exec dataplaneapi -f /etc/haproxy/dataplaneapi.yml
"""

BACKEND_CFG = """\
global
    maxconn 100
defaults
    mode http
    timeout client 5s
    timeout server 5s
    timeout connect 5s
frontend f
    bind :8080
    http-request return status 200 content-type text/plain lf-string "be={label} host=%[req.hdr(host)] xff=%[req.hdr(x-forwarded-for)] proto=%[req.hdr(x-forwarded-proto)] cf=%[req.hdr(cf-connecting-ip)] path=%[path]"
"""


def tar_of(files: dict[str, str], *, executable: set[str] | None = None) -> bytes:
    """Build a tar archive of ``files`` (path -> content) for ``put_archive("/")``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, content in files.items():
            raw = content.encode()
            info = tarfile.TarInfo(path)
            info.size = len(raw)
            info.mode = 0o755 if executable and path in executable else 0o644
            tar.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


@dataclass
class Se:
    """A running service engine."""

    name: str
    ip: str
    api_url: str
    container: Any

    def logs(self) -> str:
        """Container output, for failure messages."""
        return str(self.container.logs().decode(errors="replace"))

    def exec(self, command: str) -> str:
        """Run a shell command inside the engine and return its output."""
        result = self.container.exec_run(["sh", "-c", command])
        return str(result.output.decode(errors="replace"))


class Lab:
    """Owns the Docker network and every container started through it."""

    def __init__(self) -> None:
        """Create the network."""
        self.client = docker.from_env()
        self.network_name = f"sy-lab-{uuid.uuid4().hex[:8]}"
        pool = docker.types.IPAMPool(subnet=SUBNET, gateway="192.0.2.254")
        self._network = self.client.networks.create(
            self.network_name, ipam=docker.types.IPAMConfig(pool_configs=[pool])
        )
        self._containers: list[Any] = []
        self._scopes: list[list[Any]] = []

    def _run(
        self,
        image: str,
        ip: str,
        command: Any,
        files: dict[str, str] | None = None,
        executable: set[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        api = self.client.api
        endpoint = api.create_endpoint_config(ipv4_address=ip)
        created = api.create_container(
            image,
            command=command,
            detach=True,
            networking_config=api.create_networking_config({self.network_name: endpoint}),
            host_config=api.create_host_config(
                network_mode=self.network_name,
                port_bindings=kwargs.pop("port_bindings", None),
                sysctls=kwargs.pop("sysctls", None),
            ),
            **kwargs,
        )
        container = self.client.containers.get(created["Id"])
        if files:
            container.put_archive("/", tar_of(files, executable=executable))
        container.start()
        self._containers.append(container)
        if self._scopes:
            self._scopes[-1].append(container)
        return container

    @contextmanager
    def scope(self) -> Iterator[None]:
        """Remove every container started inside the block (and not in a nested one) on exit."""
        started: list[Any] = []
        self._scopes.append(started)
        try:
            yield
        finally:
            self._scopes.remove(started)
            for container in started:
                container.remove(force=True)
                self._containers.remove(container)

    def start_backend(self, label: str, ip: str) -> None:
        """Start a backend that answers ``be=<label> ...`` on port 8080."""
        self._run(
            BACKEND_IMAGE,
            ip,
            ["haproxy", "-f", "/be.cfg"],
            {"be.cfg": BACKEND_CFG.format(label=label)},
        )

    def start_se(
        self,
        name: str,
        ip: str,
        *,
        certs: tuple[str, ...] = ("lab",),
        nonlocal_bind: bool = True,
    ) -> Se:
        """Start a service engine and wait until its Data Plane API answers.

        ``nonlocal_bind`` mirrors the SE image's sysctl, without which HAProxy cannot bind a VIP the
        engine does not hold.
        """
        container = self._run(
            SE_IMAGE,
            ip,
            ["sh", "/seed/start.sh"],
            {
                "seed/start.sh": _START,
                "seed/reload.sh": _RELOAD,
                "seed/haproxy.cfg": _BOOTSTRAP_CFG,
                "seed/dataplaneapi.yml": _DATAPLANE_YML,
            },
            executable={"seed/reload.sh"},
            user="root",
            environment={"CERTS": " ".join(certs)},
            ports=[5555],
            port_bindings={5555: ("127.0.0.1", None)},
            sysctls={"net.ipv4.ip_nonlocal_bind": "1" if nonlocal_bind else "0"},
        )
        container.reload()
        host_port = container.attrs["NetworkSettings"]["Ports"]["5555/tcp"][0]["HostPort"]
        se = Se(name=name, ip=ip, api_url=f"http://127.0.0.1:{host_port}", container=container)
        deadline = time.monotonic() + 30
        while True:
            try:
                if httpx.get(
                    f"{se.api_url}/v3/info", auth=(API_USER, API_PASSWORD), timeout=2
                ).is_success:
                    return se
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"engine {name} did not start:\n{se.logs()}")
            time.sleep(0.3)

    def curl(self, ip: str, script: str) -> dict[str, str]:
        """Run ``script`` in a curl container with address ``ip``; return ``label -> output``.

        The script can call ``req <label> <curl args>``; each call records
        ``<label>: <body>|<status>|<redirect url>`` or ``<label>: FAIL(<curl exit code>)``.
        """
        api = self.client.api
        prelude = (
            "req() { label=$1; shift; "
            "out=$(curl -s -k --max-time 6 -w '|%{http_code}|%{redirect_url}' \"$@\") "
            '&& echo "$label: $out" || echo "$label: FAIL($?)"; }\n'
        )
        endpoint = api.create_endpoint_config(ipv4_address=ip)
        created = api.create_container(
            CURL_IMAGE,
            entrypoint=["sh", "-c"],
            command=[prelude + script],
            networking_config=api.create_networking_config({self.network_name: endpoint}),
            host_config=api.create_host_config(network_mode=self.network_name),
        )
        container = self.client.containers.get(created["Id"])
        try:
            container.start()
            container.wait()
            text = container.logs().decode()
        finally:
            container.remove(force=True)
        return dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)

    def close(self) -> None:
        """Remove every container and the network."""
        for container in self._containers:
            container.remove(force=True)
        self._network.remove()

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The real device agent against the real controller and a real HAProxy (ADR 0196).

``sy-posture`` (Go) enrolls with a one-time token and reports its facts to an API served by
uvicorn on a real PostgreSQL; the controller signs it a client certificate from its CA; HAProxy,
configured by the renderer with a TLS port that requires a certificate from that CA, answers the
device's tunnel and refuses a device certificate from another CA. After the device is revoked its
next report is refused.
"""

import asyncio
import io
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import docker
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from switchyard.adapters.pki import LocalCa
from switchyard.api.app import build_services, create_app
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.render.haproxy import render_haproxy
from switchyard.services.certs import generate_key_and_csr
from switchyard.services.devices import DeviceService
from tests.haproxy_check import HAPROXY_IMAGE
from tests.pipeline_env import TENANT, principal, world, world_data

pytestmark = pytest.mark.usefixtures("owner_engine")

POSTURE = UUID(symbolic_uuid("ps-lax"))
STRICT = UUID(symbolic_uuid("ps-strict"))
THIS_OS = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def data() -> dict[str, Any]:
    d = world_data()
    d["policy_sets"] = [
        *d.get("policy_sets", []),
        {
            "id": "ps-lax",
            "tenant_id": "t-acme",
            "name": "lax",
            "kind": "posture",
            "pki_role": "acme-devices",
            # whatever this machine is, it should pass: the check under test is the plumbing
            "requirements": {
                "require_disk_encryption": False,
                "require_firewall": False,
                "max_patch_age_days": None,
            },
        },
        {
            "id": "ps-strict",
            "tenant_id": "t-acme",
            "name": "strict",
            "kind": "posture",
            "pki_role": "acme-devices",
            "requirements": {
                "minimums": [{"os": THIS_OS, "version": "999"}],
                "require_disk_encryption": False,
                "require_firewall": False,
                "max_patch_age_days": None,
            },
        },
    ]
    return d


def self_signed_server() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    crt = cert.public_bytes(serialization.Encoding.PEM).decode()
    pem_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return crt, pem_key


def zero_trust_files(device_ca_pem: str, server_crt: str, server_key: str) -> dict[str, str]:
    d: dict[str, Any] = {
        "tenants": [{"id": "t", "name": "acme"}],
        "networks": [{"id": "n", "tenant_id": "t", "name": "lb", "cidr": "192.0.2.0/24"}],
        "se_groups": [
            {
                "id": "g",
                "tenant_id": "t",
                "name": "edge",
                "ha_mode": "vrrp",
                "networks": [{"network_id": "n", "role": "vip"}],
            }
        ],
        "certificates": [
            {
                "id": "c-srv",
                "tenant_id": "t",
                "name": "zt-server",
                "source": "upload",
                "sans": ["localhost"],
                "not_after": "2038-01-01T00:00:00+00:00",
                "secret_ref": "kv/tenants/acme/certs/zt-server",
            },
            {
                "id": "c-ca",
                "tenant_id": "t",
                "name": "device-ca",
                "source": "upload",
                "purpose": "client_ca",
                "sans": [],
                "not_after": "2038-01-01T00:00:00+00:00",
                "secret_ref": "kv/tenants/acme/certs/device-ca",
            },
        ],
        "pools": [
            {
                "id": "p",
                "tenant_id": "t",
                "name": "unused",
                "members": [{"address": "10.0.0.5", "port": 80}],
            }
        ],
        "policy_sets": [
            {
                "id": "ps-ok",
                "tenant_id": "t",
                "name": "admitted",
                "kind": "respond",
                "rules": [
                    {
                        "match": {"path_prefix": "/"},
                        "status": 200,
                        "content_type": "text/plain",
                        "body": "device-ok",
                    }
                ],
            }
        ],
        "virtual_services": [
            {
                "id": "v",
                "tenant_id": "t",
                "se_group_id": "g",
                "name": "zt",
                "protocol": "http",
                "vip": "192.0.2.30",
                "ports": [
                    {"port": 8443, "tls": True, "client_ca_id": "c-ca", "client_verify": "required"}
                ],
                "certificate_ids": ["c-srv"],
                "policy_set_ids": ["ps-ok"],
                "default_pool_id": "p",
            }
        ],
    }
    rendered = render_haproxy(snapshot_from_mapping(d), UUID(symbolic_uuid("g")))
    files = dict(rendered.files)
    cfg = files["haproxy/haproxy.cfg"]
    cfg = re.sub(r"bind 192\.0\.2\.30:8443", "bind :8443", cfg)
    cfg = re.sub(r"^\s+stats socket .*\n", "", cfg, flags=re.MULTILINE)
    files["haproxy/haproxy.cfg"] = cfg
    files["certs/zt-server.pem"] = server_crt + server_key
    files["certs/ca-device-ca.pem"] = device_ca_pem
    return files


def tar(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path, content in files.items():
            raw = content.encode()
            info = tarfile.TarInfo(f"etc/switchyard/{path}")
            info.size, info.mode = len(raw), 0o644
            archive.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def get(port: int, cafile: Path, cert: tuple[str, str] | None = None) -> str:
    """One HTTPS request to 127.0.0.1:port; returns the response text."""
    ctx = ssl.create_default_context(cafile=str(cafile))
    if cert:
        ctx.load_cert_chain(*cert)
    with (
        socket.create_connection(("127.0.0.1", port), timeout=10) as raw,
        ctx.wrap_socket(raw, server_hostname="localhost") as tls,
    ):
        tls.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        chunks = []
        while chunk := tls.recv(4096):
            chunks.append(chunk)
    return b"".join(chunks).decode(errors="replace")


async def test_the_agent_the_controller_and_haproxy_agree(pg_url: str, tmp_path: Path) -> None:
    if shutil.which("go") is None:
        pytest.skip("the Go toolchain is needed to build sy-posture")
    agent_dir = Path(__file__).parents[3] / "posture-agent"
    binary = tmp_path / ("sy-posture.exe" if os.name == "nt" else "sy-posture")
    subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/sy-posture"], cwd=agent_dir, check=True
    )

    async with world(pg_url, data()) as w:
        ca = LocalCa()
        svc = DeviceService(w.db, lambda role: ca)
        port = free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(build_services(w.db, StaticEngineDirectory({}), devices=svc)),
                host="127.0.0.1",
                port=port,
                log_level="warning",
            )
        )
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        url = f"http://127.0.0.1:{port}"
        state = tmp_path / "state"
        env = {**os.environ, "SY_POSTURE_DIR": str(state)}

        async def agent(*args: str) -> tuple[int, str]:
            done = await asyncio.to_thread(
                subprocess.run,
                [str(binary), *args],
                capture_output=True,
                text=True,
                check=False,
                env=env,
                timeout=120,
            )
            return done.returncode, done.stdout + done.stderr

        container = None
        tunnel = None
        try:
            token, _ = await svc.create_enrollment(principal(), TENANT, POSTURE, "e2e")
            code, out = await agent(
                "enroll",
                "--controller",
                url,
                "--token",
                token,
                "--name",
                "e2e-laptop",
                "--insecure-plain-http",
            )
            assert code == 0, out
            code, out = await agent(
                "enroll",
                "--controller",
                url,
                "--token",
                token,
                "--name",
                "x",
                "--dir",
                str(tmp_path / "other"),
                "--insecure-plain-http",
            )
            assert code == 1 and "401" in out, "a token works once"

            code, out = await agent("report", "--insecure-plain-http")
            assert code == 0 and "compliant" in out, out
            cert = x509.load_pem_x509_certificate((state / "tls.crt").read_bytes())
            assert cert.subject.rfc4514_string() == "CN=e2e-laptop"
            assert (state / "ca.crt").read_text() == ca.ca_pem
            assert not (state / "tls.key").read_text().startswith("-----BEGIN CERTIFICATE")
            code, out = await agent("status")
            assert code == 0 and "e2e-laptop" in out and "valid" in out, out

            # a second report works too (timestamps rise) and renews the certificate
            code, out = await agent("report", "--insecure-plain-http")
            assert code == 0, out

            # HAProxy, rendered with a TLS port that requires a certificate from this CA
            server_crt, server_key = self_signed_server()
            files = zero_trust_files(ca.ca_pem, server_crt, server_key)
            docker_client = docker.from_env()
            container = docker_client.containers.create(
                HAPROXY_IMAGE,
                command=["haproxy", "-f", "/etc/switchyard/haproxy/haproxy.cfg"],
                ports={"8443/tcp": ("127.0.0.1", None)},
                user="root",
            )
            container.put_archive("/", tar(files))
            container.start()
            time.sleep(2)
            container.reload()
            engine_port = int(
                container.attrs["NetworkSettings"]["Ports"]["8443/tcp"][0]["HostPort"]
            )
            server_ca = tmp_path / "server.pem"
            server_ca.write_text(server_crt)

            # no certificate: refused. A certificate from another CA: refused.
            with pytest.raises((ssl.SSLError, ConnectionError)):
                get(engine_port, server_ca)
            other = LocalCa()
            key_pem, csr = generate_key_and_csr(("intruder.example.test",))
            signed = await other.sign(
                csr, common_name="intruder", ip_sans=(), ttl=timedelta(hours=1), usage="client"
            )
            (tmp_path / "intruder.crt").write_text(signed.cert_pem)
            (tmp_path / "intruder.key").write_text(key_pem)
            with pytest.raises((ssl.SSLError, ConnectionError)):
                get(
                    engine_port,
                    server_ca,
                    (str(tmp_path / "intruder.crt"), str(tmp_path / "intruder.key")),
                )

            # the device, through its own tunnel
            local = free_port()
            tunnel = subprocess.Popen(
                [
                    str(binary),
                    "tunnel",
                    "--listen",
                    f"127.0.0.1:{local}",
                    "--to",
                    f"127.0.0.1:{engine_port}",
                    "--server-name",
                    "localhost",
                    "--server-ca",
                    str(server_ca),
                ],
                env=env,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", local), timeout=0.2).close()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            with socket.create_connection(("127.0.0.1", local), timeout=15) as plain:
                plain.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                reply = b""
                while chunk := plain.recv(4096):
                    reply += chunk
            assert b"200" in reply.split(b"\r\n")[0] and b"device-ok" in reply, reply

            # a non-compliant policy gets the verdict and no certificate
            strict_token, _ = await svc.create_enrollment(principal(), TENANT, STRICT)
            other_dir = tmp_path / "strict-state"
            code, out = await agent(
                "enroll",
                "--controller",
                url,
                "--token",
                strict_token,
                "--name",
                "e2e-strict",
                "--dir",
                str(other_dir),
                "--insecure-plain-http",
            )
            assert code == 0, out
            code, out = await agent("report", "--dir", str(other_dir), "--insecure-plain-http")
            assert code == 3 and "os_too_old" in out, (code, out)
            assert not (other_dir / "tls.crt").exists()

            # revoked: the next report is refused
            devices = await svc.list_devices(principal(), TENANT)
            mine = next(d for d in devices if d.name == "e2e-laptop")
            await svc.revoke(principal(), TENANT, mine.id)
            code, out = await agent("report", "--insecure-plain-http")
            assert code == 1 and "401" in out, out
        finally:
            if tunnel is not None:
                tunnel.terminate()
                tunnel.wait(10)
            if container is not None:
                container.remove(force=True)
            server.should_exit = True
            await serving

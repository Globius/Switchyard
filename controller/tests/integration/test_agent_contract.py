"""The real Go agent (cross-compiled, running in a container) against the Python adapter over mTLS."""

import datetime
import io
import ipaddress
import shutil
import subprocess
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import docker
import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from switchyard.adapters.agent import AgentUnreachableError, BundleSigner, GrpcAgent

AGENT_DIR = Path(__file__).parents[3] / "agent"
IMAGE = "alpine:3.20"

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$@" >> /tmp/systemctl.log
if [ -e /tmp/fail-reload ]; then echo "reload failed" >&2; exit 1; fi
"""
FAKE_VTYSH = """#!/bin/sh
echo "$@" >> /tmp/vtysh.log
case "$*" in
  *"show bgp summary json"*)
    echo '{"ipv4Unicast":{"peers":{"10.0.0.2":{"remoteAs":65001,"state":"Established"}}}}' ;;
esac
"""


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _issue(
    cn: str,
    key: ec.EllipticCurvePrivateKey,
    ca: tuple[x509.Certificate, ec.EllipticCurvePrivateKey],
    *,
    server: bool,
) -> x509.Certificate:
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(ca[0].subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
    )
    if server:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
    return builder.sign(ca[1], hashes.SHA256())


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _ca(cn: str) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert, key


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.signer = BundleSigner.generate()
        ca = _ca("controller-ca")
        server_key = ec.generate_private_key(ec.SECP256R1())
        client_key = ec.generate_private_key(ec.SECP256R1())
        self.ca_pem = _pem(ca[0])
        (tmp / "ca.crt").write_bytes(self.ca_pem)
        (tmp / "client.crt").write_bytes(_pem(_issue("controller", client_key, ca, server=False)))
        (tmp / "client.key").write_bytes(_key_pem(client_key))
        self.server_files = {
            "etc/switchyard/agent/tls.crt": _pem(_issue("agent", server_key, ca, server=True)),
            "etc/switchyard/agent/tls.key": _key_pem(server_key),
            "etc/switchyard/agent/ca.crt": self.ca_pem,
            "etc/switchyard/agent/bundle.pub": self.signer.public_b64().encode(),
            "usr/local/bin/systemctl": FAKE_SYSTEMCTL.encode(),
            "usr/local/bin/vtysh": FAKE_VTYSH.encode(),
        }
        other_ca = _ca("someone-else")
        other_key = ec.generate_private_key(ec.SECP256R1())
        (tmp / "evil.crt").write_bytes(_pem(_issue("evil", other_key, other_ca, server=False)))
        (tmp / "evil.key").write_bytes(_key_pem(other_key))


@pytest.fixture(scope="module")
def agent_container(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Rig, int, object]]:
    if shutil.which("go") is None:
        pytest.skip("the Go toolchain is needed to build sy-agent")
    tmp = tmp_path_factory.mktemp("agent")
    binary = tmp / "sy-agent"
    env = {"GOOS": "linux", "GOARCH": "amd64", "CGO_ENABLED": "0"}
    import os

    subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/sy-agent"],
        cwd=AGENT_DIR,
        env={**os.environ, **env},
        check=True,
    )
    rig = Rig(tmp)
    client = docker.from_env()
    container = client.containers.create(
        IMAGE,
        command=["/sy-agent", "-listen", ":7443", "-health-url", "http://127.0.0.1:1/none"],
        ports={"7443/tcp": ("127.0.0.1", None)},
    )
    try:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:

            def add(name: str, data: bytes, mode: int) -> None:
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))

            add("sy-agent", binary.read_bytes(), 0o755)
            for name, data in rig.server_files.items():
                add(name, data, 0o755 if name.startswith("usr/") else 0o644)
        container.put_archive("/", buffer.getvalue())
        container.start()
        container.reload()
        port = int(container.attrs["NetworkSettings"]["Ports"]["7443/tcp"][0]["HostPort"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if "listening" in container.logs().decode():
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(container.logs().decode())
        yield rig, port, container
    finally:
        container.remove(force=True)


def connect(
    rig: Rig, port: int, *, evil: bool = False, signer: BundleSigner | None = None
) -> GrpcAgent:
    prefix = "evil" if evil else "client"
    return GrpcAgent(
        f"localhost:{port}",
        name="se-1",
        signer=signer or rig.signer,
        ca_file=rig.tmp / "ca.crt",
        client_cert=(rig.tmp / f"{prefix}.crt", rig.tmp / f"{prefix}.key"),
    )


def sh(container: object, command: str) -> str:
    result = container.exec_run(["sh", "-c", command])  # type: ignore[attr-defined]
    return str(result.output.decode())


async def test_apply_status_and_selective_reload(agent_container: tuple[Rig, int, object]) -> None:
    rig, port, container = agent_container
    agent = connect(rig, port)
    try:
        first = await agent.apply_bundle(
            "rev-1", {"frr/frr.conf": "router bgp 65010\n", "keepalived/keepalived.conf": "vrrp\n"}
        )
        assert first.ok, first.error
        assert first.reloaded_units == ("frr", "keepalived")
        assert sh(container, "cat /etc/switchyard/frr/frr.conf") == "router bgp 65010\n"

        sh(container, "rm /tmp/systemctl.log")
        second = await agent.apply_bundle(
            "rev-2", {"frr/frr.conf": "router bgp 65011\n", "keepalived/keepalived.conf": "vrrp\n"}
        )
        assert second.ok and second.reloaded_units == ("frr",)
        assert sh(container, "cat /tmp/systemctl.log").strip() == "reload frr"

        status = await agent.status()
        assert status.bundle_id == "rev-2"
        assert set(status.file_hashes) == {"frr/frr.conf", "keepalived/keepalived.conf"}
        assert [(s.peer, s.state) for s in status.bgp] == [("10.0.0.2", "Established")]
        assert status.haproxy_healthy is False, "nothing listens on the health URL"
    finally:
        await agent.close()


async def test_failed_reload_puts_the_previous_files_back(
    agent_container: tuple[Rig, int, object],
) -> None:
    rig, port, container = agent_container
    agent = connect(rig, port)
    try:
        assert (await agent.apply_bundle("good", {"frr/frr.conf": "good\n"})).ok
        sh(container, "touch /tmp/fail-reload")
        bad = await agent.apply_bundle("bad", {"frr/frr.conf": "bad\n"})
        assert not bad.ok and bad.restored_previous
        assert sh(container, "cat /etc/switchyard/frr/frr.conf") == "good\n"
        assert (await agent.status()).bundle_id == "good"
    finally:
        sh(container, "rm -f /tmp/fail-reload")
        await agent.close()


async def test_bundles_signed_by_another_key_or_with_bad_paths_are_refused(
    agent_container: tuple[Rig, int, object],
) -> None:
    rig, port, container = agent_container
    forger = connect(rig, port, signer=BundleSigner.generate())
    honest = connect(rig, port)
    try:
        before = sh(container, "cat /etc/switchyard/frr/frr.conf")
        refused = await forger.apply_bundle("x", {"frr/frr.conf": "evil\n"})
        assert not refused.ok and "signature" in refused.error
        for path in ("../etc/passwd", "haproxy/haproxy.cfg", "agent/tls.key", "/etc/passwd"):
            result = await honest.apply_bundle("x", {path: "evil\n"})
            assert not result.ok, path
        assert sh(container, "cat /etc/switchyard/frr/frr.conf") == before
        assert "evil" not in sh(container, "cat /etc/passwd /etc/switchyard/agent/tls.key")
    finally:
        await forger.close()
        await honest.close()


async def test_clients_without_a_certificate_from_the_controller_ca_are_refused(
    agent_container: tuple[Rig, int, object],
) -> None:
    rig, port, _ = agent_container
    intruder = connect(rig, port, evil=True)
    try:
        with pytest.raises(AgentUnreachableError):
            await intruder.status()
    finally:
        await intruder.close()
    assert grpc is not None

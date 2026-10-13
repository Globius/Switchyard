# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""dnsdist in front of PowerDNS with the shipped configuration (ADR 0164).

Opt-in: ``SWITCHYARD_STACK_TESTS=1 pytest tests/integration/test_dnsdist_stack.py``. PowerDNS listens on
5300 and dnsdist (sharing its network, as in ``deploy/dns/compose.yaml``) answers on 53.
"""

import os
import socket
import struct
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from testcontainers.core.container import DockerContainer

pytestmark = pytest.mark.skipif(
    os.environ.get("SWITCHYARD_STACK_TESTS") != "1",
    reason="set SWITCHYARD_STACK_TESTS=1 to run against real PowerDNS and dnsdist",
)

CONF = Path(__file__).parents[3] / "deploy" / "dns" / "dnsdist.conf"
ZONE = "fw.example.test."


def query(port: int, name: str, qtype: int = 1, *, wait: float = 1.0) -> int | None:
    """Response code of one UDP question, or ``None`` when nothing came back (dropped)."""
    packet = (
        struct.pack(">HHHHHH", os.getpid() & 0xFFFF, 0x0100, 1, 0, 0, 0)
        + b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split("."))
        + b"\0"
        + struct.pack(">HH", qtype, 1)
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(wait)
        s.sendto(packet, ("127.0.0.1", port))
        try:
            return s.recv(4096)[3] & 15
        except OSError:
            return None


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def front() -> Iterator[int]:
    udp = free_udp_port()
    pdns = (
        DockerContainer("powerdns/pdns-auth-50:latest")
        .with_env("PDNS_AUTH_API_KEY", "k")
        .with_exposed_ports(8081)
        .with_bind_ports("53/udp", udp)
        .with_command(
            "--api=yes --webserver=yes --webserver-address=0.0.0.0 "
            "--webserver-allow-from=0.0.0.0/0 --local-port=5300"
        )
    )
    with pdns:
        api = f"http://127.0.0.1:{pdns.get_exposed_port(8081)}/api/v1/servers/localhost/zones"
        for _ in range(60):
            try:
                if httpx.post(
                    api,
                    headers={"X-API-Key": "k"},
                    json={"name": ZONE, "kind": "Native", "nameservers": ["ns1.fw.example.test."]},
                ).status_code in {201, 409}:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        cid = pdns.get_wrapped_container().id
        dist = (
            DockerContainer("powerdns/dnsdist-19:latest")
            .with_kwargs(network_mode=f"container:{cid}")
            .with_volume_mapping(str(CONF), "/etc/dnsdist/dnsdist.conf", "ro")
            .with_env("DNSDIST_BACKEND", "127.0.0.1:5300")
            .with_env("DNSDIST_QPS_LIMIT", "5")
            .with_env("DNSDIST_QPS_BURST", "5")
            .with_env("DNSDIST_BLOCK_SECONDS", "5")
            .with_command("--supervised --disable-syslog")
        )
        with dist:
            for _ in range(60):
                if query(udp, ZONE, 6, wait=0.5) == 0:
                    break
                time.sleep(1)
            yield udp


def test_normal_questions_are_answered_through_dnsdist(front: int) -> None:
    time.sleep(2)  # let the per-network budget refill after the readiness probes
    assert query(front, ZONE, 6) == 0  # SOA
    assert query(front, "missing." + ZONE, 1) == 3  # NXDOMAIN, from PowerDNS


def test_any_and_transfers_are_refused(front: int) -> None:
    time.sleep(2)
    assert query(front, ZONE, 255) == 5  # ANY: REFUSED
    time.sleep(1)
    assert query(front, ZONE, 252) == 5  # AXFR over UDP: REFUSED
    time.sleep(1)
    assert query(front, ZONE, 251) == 5  # IXFR


def test_a_flood_from_one_network_is_dropped_not_answered(front: int) -> None:
    time.sleep(3)
    answers = [query(front, ZONE, 6, wait=0.15) for _ in range(60)]
    got = [a for a in answers if a is not None]
    assert 0 < len(got) < 40, (
        f"{len(got)} of 60 answered"
    )  # some pass (the burst), the rest are dropped
    assert None in answers

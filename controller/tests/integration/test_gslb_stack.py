# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""GSLB against a real PowerDNS 5 (ADR 0160): the records the controller writes answer as designed.

Opt-in (it pulls an image): ``SWITCHYARD_STACK_TESTS=1 pytest tests/integration/test_gslb_stack.py``.
PowerDNS checks health itself, from inside its container: the members are a web server container
(up) and unused addresses on the same Docker network (down).
"""

import os
import random
import socket
import struct
import time
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from testcontainers.core.container import DockerContainer

from switchyard.adapters.powerdns import HttpPowerDns
from switchyard.domain.workflow import Role
from switchyard.services.context import Principal
from switchyard.services.gslb import GslbService
from tests.pipeline_env import TENANT, World, world

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("SWITCHYARD_STACK_TESTS") != "1",
        reason="set SWITCHYARD_STACK_TESTS=1 to run against a real PowerDNS",
    ),
    pytest.mark.usefixtures("owner_engine"),
]

ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
ADMIN = Principal(actor="user:alice", role=Role.TENANT_ADMIN, tenant_ids=frozenset({TENANT}))
ZONE = "gslb.example.test"
# Set by the fixture: a web server that is up, and two addresses on the same Docker network that
# nobody answers for (a connect times out), so "down" does not depend on the host network.
UP = DOWN = DOWN2 = ""


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


def ask(name: str, port: int, qtype: int = 1) -> tuple[int, list[str]]:
    """One DNS question over UDP; returns the response code and the A records."""
    tid = random.randint(0, 65535)  # noqa: S311 (a DNS transaction id, not a secret)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"
    packet = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", qtype, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(3)
        s.sendto(packet, ("127.0.0.1", port))
        data = s.recv(4096)
    answers = struct.unpack(">H", data[6:8])[0]
    i = 12
    while data[i] != 0:
        i += data[i] + 1
    i += 5
    out: list[str] = []
    for _ in range(answers):
        if data[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while data[i] != 0:
                i += data[i] + 1
            i += 1
        rtype, _cls, _ttl, length = struct.unpack(">HHIH", data[i : i + 10])
        i += 10
        if rtype == 1:
            out.append(".".join(str(b) for b in data[i : i + 4]))
        i += length
    return data[3] & 15, out


def eventually(name: str, port: int, want: list[str], seconds: int = 90) -> list[str]:
    deadline = time.monotonic() + seconds
    got: list[str] = []
    while time.monotonic() < deadline:
        _, got = ask(name, port)
        if sorted(got) == sorted(want):
            return got
        time.sleep(1)
    return got


class Pdns:
    def __init__(self, api: str, port: int) -> None:
        self.api, self.port = api, port


@pytest.fixture(scope="module")
def pdns() -> Iterator[Pdns]:
    udp = free_udp_port()
    container = (
        DockerContainer("powerdns/pdns-auth-50:latest")
        .with_env("PDNS_AUTH_API_KEY", "stack-key")
        .with_exposed_ports(8081)
        .with_bind_ports("53/udp", udp)
        .with_command(
            "--api=yes --webserver=yes --webserver-address=0.0.0.0 --webserver-allow-from=0.0.0.0/0 "
            "--enable-lua-records=yes --lua-health-checks-interval=2"
        )
    )
    web = DockerContainer("busybox:1.36").with_command("httpd -f -p 8080")
    with container, web:
        global UP, DOWN, DOWN2
        inner = web.get_wrapped_container()
        inner.reload()
        UP = inner.attrs["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
        prefix = ".".join(UP.split(".")[:2])
        DOWN, DOWN2 = f"{prefix}.200.1", f"{prefix}.200.2"
        api = f"http://127.0.0.1:{container.get_exposed_port(8081)}"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if (
                    httpx.get(
                        api + "/api/v1/servers/localhost", headers={"X-API-Key": "stack-key"}
                    ).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        for _ in range(60):  # UDP is mapped a moment after the API answers
            try:
                ask("warmup.example.test", udp)
                break
            except OSError:
                time.sleep(1)
        yield Pdns(api, udp)


@pytest_asyncio.fixture
async def svc(pdns: Pdns, pg_url: str) -> AsyncIterator[tuple[GslbService, World]]:
    async with world(pg_url) as w:
        service = GslbService(w.db, HttpPowerDns(pdns.api, "stack-key"))
        await service.create_zone(ROOT, TENANT, ZONE, ["ns1.example.test"])
        yield service, w


async def test_failover_answers_the_first_tier_that_is_up(
    svc: tuple[GslbService, World], pdns: Pdns
) -> None:
    service, _ = svc
    await service.create_wide_ip(
        ADMIN,
        TENANT,
        ZONE,
        "fail",
        {
            "algorithm": "failover",
            "health": {"port": 8080, "timeout_ms": 200},
            "members": [{"address": DOWN, "priority": 1}, {"address": UP, "priority": 2}],
        },
    )
    assert eventually(f"fail.{ZONE}", pdns.port, [UP]) == [UP]  # priority 1 is down: tier 2 answers


async def test_multivalue_and_weighted_skip_members_that_are_down(
    svc: tuple[GslbService, World], pdns: Pdns
) -> None:
    service, _ = svc
    members = [
        {"address": UP, "weight": 5},
        {"address": DOWN, "weight": 5},
        {"address": DOWN2, "weight": 1},
    ]
    await service.create_wide_ip(
        ADMIN,
        TENANT,
        ZONE,
        "all",
        {"algorithm": "all", "health": {"port": 8080, "timeout_ms": 200}, "members": members},
    )
    await service.create_wide_ip(
        ADMIN,
        TENANT,
        ZONE,
        "weighted",
        {"algorithm": "weighted", "health": {"port": 8080, "timeout_ms": 200}, "members": members},
    )
    assert eventually(f"all.{ZONE}", pdns.port, [UP]) == [UP]
    assert eventually(f"weighted.{ZONE}", pdns.port, [UP]) == [UP]
    # when everything is down, the answer is still all members (better than no answer)
    await service.create_wide_ip(
        ADMIN,
        TENANT,
        ZONE,
        "dead",
        {
            "algorithm": "weighted",
            "health": {"port": 8080, "timeout_ms": 200},
            "members": [{"address": DOWN}, {"address": DOWN2}],
        },
    )
    code, got = ask(f"dead.{ZONE}", pdns.port)
    time.sleep(4)
    code, got = ask(f"dead.{ZONE}", pdns.port)
    assert code == 0 and got and set(got) <= {DOWN, DOWN2}


async def test_subnet_answers_by_the_clients_network(
    svc: tuple[GslbService, World], pdns: Pdns
) -> None:
    service, _ = svc
    await service.create_wide_ip(
        ADMIN,
        TENANT,
        ZONE,
        "view",
        {
            "algorithm": "subnet",
            "health": {"port": 8080, "timeout_ms": 200},
            "members": [{"address": UP, "subnets": ["128.0.0.0/1"]}, {"address": DOWN2}],
        },
    )
    # the question arrives from a Docker address, 128.0.0.0/1 on every host this runs on
    assert eventually(f"view.{ZONE}", pdns.port, [UP]) == [UP]
    await service.update_wide_ip(
        ADMIN,
        TENANT,
        ZONE,
        "view",
        spec={"members": [{"address": UP, "subnets": ["10.0.0.0/8"]}, {"address": DOWN2}]},
    )
    time.sleep(3)
    code, got = ask(f"view.{ZONE}", pdns.port)
    assert code == 0 and got == [
        DOWN2
    ]  # not in 10/8: the default member (which is down; all is shown)


async def test_removing_a_wide_ip_removes_the_answer_and_dnssec_signs_the_zone(
    svc: tuple[GslbService, World], pdns: Pdns
) -> None:
    service, _ = svc
    await service.create_wide_ip(
        ADMIN,
        TENANT,
        ZONE,
        "gone",
        {
            "algorithm": "all",
            "health": {"port": 8080, "timeout_ms": 200},
            "members": [{"address": UP}],
        },
    )
    assert eventually(f"gone.{ZONE}", pdns.port, [UP]) == [UP]
    await service.delete_wide_ip(ADMIN, TENANT, ZONE, "gone")
    assert ask(f"gone.{ZONE}", pdns.port)[0] == 3  # NXDOMAIN
    signed = await service.enable_dnssec(ADMIN, TENANT, ZONE)
    assert signed["dnssec"] is True and signed["ds"] and " IN DS " in signed["ds"][0]
    assert (await service.ds_records(ADMIN, TENANT, ZONE))["ds"] == signed["ds"]
    states = await service.status(ADMIN, TENANT)
    assert all(s["state"] in {"in_sync", "off"} for s in states)


async def test_weighted_and_failover_honour_http_health_checks(
    svc: tuple[GslbService, World], pdns: Pdns
) -> None:
    """``ifurlup`` needs a list of address sets; a flat list made weighted ignore health."""
    service, _ = svc
    health = {
        "kind": "http",
        "scheme": "http",
        "port": 8080,
        "path": "/etc/hostname",
        "timeout_ms": 1000,
    }
    members = [{"address": DOWN, "weight": 5}, {"address": UP, "weight": 5}]
    await service.create_wide_ip(
        ADMIN,
        TENANT,
        ZONE,
        "wurl",
        {"algorithm": "weighted", "health": health, "members": members},
    )
    await service.create_wide_ip(
        ADMIN,
        TENANT,
        ZONE,
        "furl",
        {
            "algorithm": "failover",
            "health": health,
            "members": [{"address": DOWN, "priority": 1}, {"address": UP, "priority": 2}],
        },
    )
    assert eventually(f"wurl.{ZONE}", pdns.port, [UP]) == [UP]
    assert eventually(f"furl.{ZONE}", pdns.port, [UP]) == [UP]
    # A member nobody has checked yet is assumed up, so the first answers can include the down one;
    # once the checks have run it is never picked.
    time.sleep(8)
    assert {ask(f"wurl.{ZONE}", pdns.port)[1][0] for _ in range(20)} == {UP}

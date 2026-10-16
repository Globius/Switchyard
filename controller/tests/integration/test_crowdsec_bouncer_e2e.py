# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A real CrowdSec local API, the real SPOA bouncer and real HAProxy: a ban blocks, removal unblocks (ADR 0215).

Everything runs in the Docker lab (RFC 5737 range 192.0.2.0/24, see ``tests/lab.py``):

- 192.0.2.30  a CrowdSec security engine (local API only, no connection to the Central API)
- 192.0.2.20  an engine: HAProxy with the rendered config and the bouncer with the rendered bundle file
- 192.0.2.51  a backend
- 192.0.2.98/99  clients (curl); only .98 gets banned

The bouncer binary is the release the SE image pins (``se-image/versions.env``), downloaded once and
checked against the pinned SHA-256.
"""

import hashlib
import io
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.render.crowdsec import BOUNCER_PATH, BouncerSettings, render_bouncer_files
from switchyard.render.haproxy import render_haproxy
from tests.haproxy_check import HAPROXY_IMAGE
from tests.lab import SUBNET, Lab

CROWDSEC_IMAGE = "crowdsecurity/crowdsec:v1.7.0"
EDGE, LAPI, BACKEND = "192.0.2.20", "192.0.2.30", "192.0.2.51"
BANNED, OTHER = "192.0.2.98", "192.0.2.99"
KEY = "e2e-bouncer-key-0123456789abcdef"
VERSIONS = Path(__file__).parents[3] / "se-image" / "versions.env"


def pinned(name: str) -> str:
    for line in VERSIONS.read_text("utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise KeyError(name)


def bouncer_binary() -> bytes:
    """The pinned SPOA bouncer release, from a cache or GitHub, verified against its checksum."""
    version, digest = pinned("CROWDSEC_SPOA_VERSION"), pinned("CROWDSEC_SPOA_SHA256_LINUX_AMD64")
    cache = Path(tempfile.gettempdir()) / f"crowdsec-spoa-bouncer-{version}.tgz"
    if not cache.exists():
        url = (
            "https://github.com/crowdsecurity/cs-haproxy-spoa-bouncer/releases/download/"
            f"{version}/crowdsec-spoa-bouncer-linux-amd64.tgz"
        )
        cache.write_bytes(urllib.request.urlopen(url, timeout=120).read())  # noqa: S310
    raw = cache.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        cache.unlink()
        raise AssertionError("the bouncer download does not match the pinned checksum")
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        member = tar.extractfile(f"crowdsec-spoa-bouncer-{version}/crowdsec-spoa-bouncer")
        assert member is not None
        return member.read()


def data() -> dict[str, Any]:
    def service(name: str, port: int, policy: str) -> dict[str, Any]:
        return {
            "id": f"v-{name}",
            "tenant_id": "t",
            "se_group_id": "g",
            "name": name,
            "protocol": "http",
            "vip": EDGE,
            "ports": [{"port": port}],
            "policy_set_ids": [policy],
            "default_pool_id": "pool",
        }

    def policy(name: str, **extra: Any) -> dict[str, Any]:
        return {"id": f"ps-{name}", "tenant_id": "t", "name": name, "kind": "crowdsec", **extra}

    return {
        "tenants": [{"id": "t", "name": "acme"}],
        "networks": [
            {"id": "n", "tenant_id": "t", "name": "lab", "cidr": SUBNET},
            {"id": "m", "tenant_id": "t", "name": "mgmt", "cidr": "192.0.2.0/25"},
        ],
        "se_groups": [
            {
                "id": "g",
                "tenant_id": "t",
                "name": "edge",
                "ha_mode": "vrrp",
                "networks": [
                    {"network_id": "n", "role": "vip"},
                    {"network_id": "m", "role": "mgmt"},
                ],
            }
        ],
        "pools": [
            {
                "id": "pool",
                "tenant_id": "t",
                "name": "pool",
                "members": [{"address": BACKEND, "port": 8080}],
            }
        ],
        "policy_sets": [
            policy("block", mode="block"),
            policy("detect"),
            policy("allowed", mode="block", allow_cidrs=[f"{BANNED}/32"]),
            policy("closed", mode="block", fail_mode="closed"),
        ],
        "virtual_services": [
            service("blocking", 80, "ps-block"),
            service("detecting", 8081, "ps-detect"),
            service("allowlisted", 8082, "ps-allowed"),
            service("closed", 8083, "ps-closed"),
        ],
    }


EDGE_START = r"""
set -eu
mkdir -p /run/haproxy
/usr/local/bin/crowdsec-spoa-bouncer -c /etc/switchyard/crowdsec/spoa-bouncer.yaml -t
/usr/local/bin/crowdsec-spoa-bouncer -c /etc/switchyard/crowdsec/spoa-bouncer.yaml > /tmp/bouncer.log 2>&1 &
sleep 2
exec haproxy -f /etc/switchyard/haproxy/haproxy.cfg
"""


def exec_in(container: Any, command: str) -> str:
    result = container.exec_run(["sh", "-c", command])
    return str(result.output.decode(errors="replace"))


@pytest.fixture(scope="module")
def stack(lab_network: Lab) -> Iterator[tuple[Any, Any]]:
    binary = bouncer_binary()
    with lab_network.scope():
        lab_network.start_backend("A", BACKEND)
        lapi = lab_network._run(
            CROWDSEC_IMAGE,
            LAPI,
            None,
            environment={
                "DISABLE_ONLINE_API": "true",
                "BOUNCER_KEY_switchyard": KEY,
                "DISABLE_AGENT": "true",
                "CROWDSEC_BYPASS_DB_VOLUME_CHECK": "true",  # a throwaway database
            },
            user="root",
        )
        deadline = time.monotonic() + 120
        while (
            exec_in(lapi, "cscli bouncers list -o raw 2>/dev/null | grep -c switchyard").strip()
            != "1"
        ):
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"CrowdSec did not start:\n{lapi.logs().decode(errors='replace')}"
                )
            time.sleep(2)
        snapshot = snapshot_from_mapping(data())
        group = UUID(symbolic_uuid("g"))
        rendered = render_haproxy(snapshot, group)
        settings = BouncerSettings(f"http://{LAPI}:8080", KEY, ())
        bundle = render_bouncer_files(snapshot, group, settings)
        files: dict[str, str | bytes] = {
            f"etc/switchyard/{p}": c for p, c in rendered.files.items()
        }
        files[f"etc/switchyard/{BOUNCER_PATH}"] = bundle[BOUNCER_PATH]
        files["usr/local/bin/crowdsec-spoa-bouncer"] = binary
        edge = lab_network._run(
            HAPROXY_IMAGE,
            EDGE,
            ["sh", "-c", EDGE_START],
            files,
            executable={"usr/local/bin/crowdsec-spoa-bouncer"},
            user="root",
        )
        time.sleep(5)
        yield lab_network, (lapi, edge)


def get(lab: Lab, client: str) -> dict[str, str]:
    script = "\n".join(
        f"req {label} -o /dev/null http://{EDGE}:{port}/"
        for label, port in (("block", 80), ("detect", 8081), ("allowed", 8082), ("closed", 8083))
    )
    return lab.curl(client, script)


def status(result: dict[str, str], label: str) -> str:
    parts = result[label].rsplit("|", 2)
    return parts[1] if len(parts) == 3 else result[label]


def wait_for(lab: Lab, client: str, label: str, want: str, seconds: float = 45) -> dict[str, str]:
    deadline = time.monotonic() + seconds
    while True:
        got = get(lab, client)
        if status(got, label) == want or time.monotonic() > deadline:
            return got
        time.sleep(3)


def test_a_ban_blocks_and_removing_it_unblocks(stack: tuple[Lab, tuple[Any, Any]]) -> None:
    lab, (lapi, edge) = stack
    clean = get(lab, BANNED)
    assert [status(clean, k) for k in ("block", "detect", "allowed", "closed")] == ["200"] * 4, (
        clean,
        exec_in(edge, "cat /tmp/bouncer.log"),
    )

    exec_in(lapi, f"cscli decisions add --ip {BANNED} --duration 1h --reason e2e")
    banned = wait_for(lab, BANNED, "block", "403")
    log = exec_in(edge, "cat /tmp/bouncer.log")
    assert status(banned, "block") == "403", (banned, log)
    # detect mode logs the verdict but lets the request through; the allowlisted client is never checked
    assert status(banned, "detect") == "200" and status(banned, "allowed") == "200"
    assert status(banned, "closed") == "403"
    # someone else is not affected
    assert status(get(lab, OTHER), "block") == "200"

    exec_in(lapi, f"cscli decisions delete --ip {BANNED}")
    freed = wait_for(lab, BANNED, "block", "200")
    assert status(freed, "block") == "200", (freed, exec_in(edge, "cat /tmp/bouncer.log"))


def test_when_the_bouncer_is_gone_open_services_serve_and_closed_ones_refuse(
    stack: tuple[Lab, tuple[Any, Any]],
) -> None:
    lab, (_, edge) = stack
    # the image has no pkill: find the process through /proc
    exec_in(
        edge,
        "for p in /proc/[0-9]*; do grep -q crowdsec-spoa-bouncer $p/cmdline 2>/dev/null"
        " && kill ${p#/proc/}; done; true",
    )
    time.sleep(2)
    got = get(lab, OTHER)
    assert status(got, "block") == "200" and status(got, "detect") == "200"
    assert status(got, "closed") == "503"

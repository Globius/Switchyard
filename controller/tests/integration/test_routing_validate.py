# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""keepalived and FRR accept every rendered routing config (real binaries in containers)."""

import io
import tarfile
from pathlib import Path

import docker
import pytest

from tests.routing_support import case_names, render_case

SE_TOOLS = "switchyard-test/se-tools:trixie"
FRR = "quay.io/frrouting/frr:10.2.1"


def se_tools_image() -> str:
    """Build (once, then cached) the image with the SE image's keepalived, ipvsadm and nftables."""
    client = docker.from_env()
    path = Path(__file__).parent.parent / "images" / "se-tools"
    client.images.build(path=str(path), tag=SE_TOOLS, rm=True)
    return SE_TOOLS


def run(image: str, path: str, content: str, command: str) -> tuple[int, str]:
    client = docker.from_env()
    container = client.containers.create(
        image, entrypoint=["sh", "-c", command], network_mode="none", privileged=True
    )
    try:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            data = content.encode()
            info = tarfile.TarInfo(path.lstrip("/"))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        container.put_archive("/", buffer.getvalue())
        container.start()
        status = container.wait()["StatusCode"]
        return int(status), container.logs().decode(errors="replace")
    finally:
        container.remove(force=True)


# keepalived checks that the script user and the interface exist, so the sandbox creates stand-ins
_KEEPALIVED = (
    "echo keepalived_script:x:999:999::/:/bin/false >> /etc/passwd; "
    "iface=$(awk '$1==\"interface\"{print $2; exit}' /tmp/k.conf); "
    'ip link add "${iface:-eth9}" type dummy; ip link set "${iface:-eth9}" multicast on up; '
    "keepalived --config-test -f /tmp/k.conf 2>&1"
)


def keepalived_ok(conf: str) -> tuple[bool, str]:
    code, out = run(se_tools_image(), "/tmp/k.conf", conf, _KEEPALIVED)  # noqa: S108
    problems = (
        "Unknown keyword",
        "Error",
        "Unable",
        "Non-existent",
        "not found",
        "Disabling",
        "WARNING",
        "Warning",
    )
    return code == 0 and not any(p in out for p in problems), out


def nft_ok(rules: str) -> tuple[bool, str]:
    code, out = run(se_tools_image(), "/tmp/r.nft", rules, "nft -c -f /tmp/r.nft 2>&1")  # noqa: S108
    return code == 0, out


def frr_ok(conf: str) -> tuple[bool, str]:
    _, out = run(FRR, "/etc/frr/frr.conf", conf, "vtysh -C 2>&1")
    bad = [
        line
        for line in out.splitlines()
        if line.startswith("line ") or ("% " in line and "vtysh.conf" not in line)
    ]
    return not bad, out


@pytest.mark.parametrize("name", case_names())
def test_every_rendered_file_is_accepted(name: str) -> None:
    for engine, files in render_case(name).items():
        if "keepalived/keepalived.conf" in files:
            ok, out = keepalived_ok(files["keepalived/keepalived.conf"])
            assert ok, f"{name}/{engine}: {out}"
        if "nftables/switchyard-udp.nft" in files:
            ok, out = nft_ok(files["nftables/switchyard-udp.nft"])
            assert ok, f"{name}/{engine}: {out}"
        if "frr/frr.conf" in files:
            ok, out = frr_ok(files["frr/frr.conf"])
            assert ok, f"{name}/{engine}: {out}"


def test_the_checkers_do_reject_garbage() -> None:
    assert not keepalived_ok("vrrp_instance x {\n  bogus_keyword 1\n}\n")[0]
    assert not frr_ok("router bgp 1\n bogus thing\n")[0]

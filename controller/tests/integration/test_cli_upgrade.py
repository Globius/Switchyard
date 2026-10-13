# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``syctl upgrade check | plan`` against a real server (ADR 0205)."""

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from switchyard.cli import app as cli
from tests.integration import test_syctl_api
from tests.integration.test_syctl_api import Server
from tests.unit.test_cli_release import make_bundle

pytestmark = pytest.mark.usefixtures("owner_engine")

server = test_syctl_api.server  # the same fixture: a real controller over HTTP


async def run(server: Server, *args: str) -> tuple[int, str]:
    result = await asyncio.to_thread(CliRunner().invoke, cli, list(args))
    return result.exit_code, result.output


async def test_check_and_plan(server: Server, tmp_path: Path) -> None:
    code, out = await run(server, "api", "login", "--url", server.url, "--token", server.token)
    assert code == 0, out
    code, out = await run(server, "upgrade", "check")
    assert code == 0, out
    assert "[current]" in out and "No configuration backup" in out

    bundle, trust = await asyncio.to_thread(make_bundle, tmp_path)
    code, out = await run(server, "upgrade", "plan", str(bundle), "--trust", trust)
    assert code == 0, out
    assert "bundle: switchyard 0.2.0" in out and "5. roll engines" in out
    assert "installs over" not in out  # the test controller runs 0.1.0, which the bundle lists

    code, out = await run(server, "upgrade", "plan", str(bundle))
    assert code == 1 and "REFUSED" in out


async def test_apply_on_a_node(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from switchyard import cli_upgrade
    from tests.unit.test_upgrade_apply import Fake

    fake = Fake()
    monkeypatch.setattr(cli_upgrade, "APPLY_RUNNER", fake)
    # a bundle that carries images, signed by a key the command is told to trust
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from switchyard.domain.releases import public_key_b64
    from switchyard.services.release_bundle import Input, build

    key = Ed25519PrivateKey.generate()
    (tmp_path / "controller.tar").write_bytes(b"c")
    bundle = tmp_path / "b.sybundle"
    build(
        bundle,
        version="0.2.0",
        schema_head="0046",
        created_at="2026-10-13T04:00:00Z",
        inputs=[Input("controller-image", tmp_path / "controller.tar", "controller.tar")],
        key=key,
        key_id="k1",
    )
    env = tmp_path / "node.env"
    env.write_text("CONTROLLER_IMAGE=switchyard-controller:0.1.0\n")
    args = [
        "upgrade", "apply", str(bundle), "--compose-file", str(tmp_path / "c.yaml"),
        "--env-file", str(env), "--health-url", server.url, "--workdir", str(tmp_path / "w"),
        "--trust", f"k1={public_key_b64(key)}", "--yes",
    ]  # fmt: skip
    code, out = await run(server, *args)
    assert code == 1 and "--backup-target" in out  # a backup, or an explicit --no-backup
    code, out = await run(server, *args, "--no-backup")
    assert code == 0, out
    assert "ok: node healthy" in out
    assert "switchyard-controller:0.2.0" in env.read_text()

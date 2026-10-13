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

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""deploy/crowdsec/register-engine.sh registers one bouncer per engine and keeps the key off command lines (ADR 0215)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[3] / "deploy" / "crowdsec" / "register-engine.sh"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(
    SH is None or shutil.which("openssl") is None, reason="needs sh and openssl"
)

STUB = """#!/bin/sh
printf '%s\n' "$*" >> "$LOG"
if [ -n "${STDIN_TO:-}" ]; then cat > "$STDIN_TO"; fi
"""


def run(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    stub = tmp_path / "stub.sh"
    stub.write_text(STUB, "utf-8")
    log = tmp_path / "calls.log"
    stdin_to = tmp_path / "bao-stdin"
    cscli = tmp_path / "cscli"
    bao = tmp_path / "bao"
    cscli.write_text(f'#!/bin/sh\nLOG="{log}" sh "{stub}" cscli "$@"\n', "utf-8")
    bao.write_text(f'#!/bin/sh\nLOG="{log}" STDIN_TO="{stdin_to}" sh "{stub}" bao "$@"\n', "utf-8")
    for tool in (cscli, bao):
        tool.chmod(0o755)
    assert SH is not None
    result = subprocess.run(
        [SH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "CSCLI": str(cscli), "BAO": str(bao)},
        check=False,
    )
    calls = log.read_text("utf-8").splitlines() if log.exists() else []
    stdin = stdin_to.read_text("utf-8") if stdin_to.exists() else ""
    return result, calls, stdin


def test_add_registers_the_bouncer_and_stores_the_same_key_without_printing_it(
    tmp_path: Path,
) -> None:
    result, calls, stdin = run(tmp_path, "add", "edge-1")
    assert result.returncode == 0, result.stderr
    key = stdin
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert calls[0] == f"cscli bouncers add edge-1 -k {key}"
    assert calls[1] == "bao kv put kv/system/crowdsec/bouncer-keys/edge-1 value=-"
    assert key not in result.stdout + result.stderr
    assert key not in calls[1]  # OpenBao gets it on standard input, not on a command line


def test_revoke_removes_the_bouncer_and_the_stored_key(tmp_path: Path) -> None:
    result, calls, _ = run(tmp_path, "revoke", "edge-1")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "cscli bouncers delete edge-1",
        "bao kv metadata delete kv/system/crowdsec/bouncer-keys/edge-1",
    ]


@pytest.mark.parametrize(
    "args", [(), ("add",), ("add", "Edge_1"), ("add", "-x"), ("add", "a b"), ("nope", "edge-1")]
)
def test_bad_arguments_change_nothing(tmp_path: Path, args: tuple[str, ...]) -> None:
    result, calls, _ = run(tmp_path, *args)
    assert result.returncode == 2 and calls == []

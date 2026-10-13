# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``syctl release keygen | build | verify`` (ADR 0205)."""

import re
from pathlib import Path

from typer.testing import CliRunner

from switchyard.cli import app

runner = CliRunner()


def make_bundle(tmp_path: Path) -> tuple[Path, str]:
    key = tmp_path / "release.pem"
    res = runner.invoke(app, ["release", "keygen", str(key), "--key-id", "ci"])
    assert res.exit_code == 0, res.output
    trust = re.search(r"--trust (\S+)", res.output).group(1)  # type: ignore[union-attr]
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n")
    notes = tmp_path / "notes.json"
    notes.write_text('{"breaking": ["Removed the old flag"]}')
    out = tmp_path / "sw.sybundle"
    res = runner.invoke(
        app,
        ["release", "build", str(out), "--key", str(key), "--key-id", "ci", "--version", "0.2.0",
         "--compose", str(compose), "--upgrade-from", "0.1.0", "--notes", str(notes)],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    return out, trust


def test_keygen_build_verify(tmp_path: Path) -> None:
    out, trust = make_bundle(tmp_path)
    res = runner.invoke(app, ["release", "verify", str(out), "--trust", trust])
    assert res.exit_code == 0, res.output
    assert "switchyard 0.2.0" in res.output and "installs over: 0.1.0" in res.output
    assert "[breaking] Removed the old flag" in res.output


def test_verify_refuses_without_the_key_or_with_a_changed_file(tmp_path: Path) -> None:
    out, trust = make_bundle(tmp_path)
    res = runner.invoke(app, ["release", "verify", str(out)])
    assert res.exit_code == 1 and "REFUSED" in res.output and "not trusted" in res.output
    data = bytearray(out.read_bytes())
    data[data.index(b"services: {}") + 10] ^= 0x01
    out.write_bytes(bytes(data))
    res = runner.invoke(app, ["release", "verify", str(out), "--trust", trust])
    assert res.exit_code == 1 and "REFUSED" in res.output


def test_keygen_does_not_overwrite_and_build_needs_files(tmp_path: Path) -> None:
    key = tmp_path / "k.pem"
    key.write_text("keep me")
    res = runner.invoke(app, ["release", "keygen", str(key)])
    assert res.exit_code == 1 and key.read_text() == "keep me"
    res = runner.invoke(app, ["release", "build", str(tmp_path / "o"), "--key", str(key)])
    assert res.exit_code == 1 and "at least one file" in res.output

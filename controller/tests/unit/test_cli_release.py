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


def _bundle(tmp_path: Path, key: Path, version: str, name: str) -> Path:
    compose = tmp_path / f"compose-{name}.yaml"
    compose.write_text(f"# {version}\n")
    out = tmp_path / f"{name}.sybundle"
    res = runner.invoke(
        app,
        ["release", "build", str(out), "--key", str(key), "--key-id", "ci", "--version", version,
         "--compose", str(compose), "--upgrade-from", "0.1.0"],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    return out


def test_feed_is_signed_verifiable_by_a_controller_and_extended_by_the_next_release(
    tmp_path: Path,
) -> None:
    import json

    from switchyard.domain.releases import latest, upgrade_path, verify_feed

    key = tmp_path / "release.pem"
    res = runner.invoke(app, ["release", "keygen", str(key), "--key-id", "ci"])
    trust = re.search(r"--trust ci=(\S+)", res.output).group(1)  # type: ignore[union-attr]
    first = _bundle(tmp_path, key, "0.2.0", "a")
    feed = tmp_path / "releases.json"
    res = runner.invoke(
        app,
        ["release", "feed", str(feed), "--key", str(key), "--key-id", "ci",
         "--bundle", f"{first}=https://example.test/a.sybundle"],
    )  # fmt: skip
    assert res.exit_code == 0, res.output

    parsed = verify_feed(json.loads(feed.read_text()), {"ci": trust})
    (release,) = parsed.releases
    assert release.version == "0.2.0" and release.upgrade_from == ["0.1.0"]
    assert release.bundle is not None and release.bundle.url == "https://example.test/a.sybundle"
    assert release.bundle.size_bytes == first.stat().st_size

    second = _bundle(tmp_path, key, "0.3.0", "b")
    res = runner.invoke(
        app,
        ["release", "feed", str(feed), "--key", str(key), "--key-id", "ci",
         "--previous", str(feed), "--bundle", f"{second}=https://example.test/b.sybundle"],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    parsed = verify_feed(json.loads(feed.read_text()), {"ci": trust})
    assert [r.version for r in parsed.releases] == ["0.2.0", "0.3.0"]
    top = latest(parsed)
    assert top is not None and top.version == "0.3.0"
    assert upgrade_path(parsed, "0.1.0", "0.3.0")[-1] == "0.3.0"


def test_feed_refuses_other_keys_plain_http_and_foreign_earlier_feeds(tmp_path: Path) -> None:
    key, other = tmp_path / "k.pem", tmp_path / "o.pem"
    runner.invoke(app, ["release", "keygen", str(key), "--key-id", "ci"])
    runner.invoke(app, ["release", "keygen", str(other), "--key-id", "ci"])
    bundle = _bundle(tmp_path, key, "0.2.0", "a")
    feed = tmp_path / "f.json"
    base = ["release", "feed", str(feed), "--key-id", "ci"]
    res = runner.invoke(app, [*base, "--key", str(other), "--bundle", f"{bundle}=https://x.test/a"])
    assert res.exit_code == 1  # the bundle is signed with another key
    res = runner.invoke(app, [*base, "--key", str(key), "--bundle", f"{bundle}=http://x.test/a"])
    assert res.exit_code == 1 and "https" in res.output
    ok = runner.invoke(app, [*base, "--key", str(key), "--bundle", f"{bundle}=https://x.test/a"])
    assert ok.exit_code == 0
    res = runner.invoke(
        app,
        [
            *base,
            "--key",
            str(other),
            "--previous",
            str(feed),
            "--bundle",
            f"{bundle}=https://x.test/a",
        ],
    )
    assert res.exit_code == 1  # an earlier feed from another signer is never re-signed

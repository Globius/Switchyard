# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Quickstart credentials grant access only to the operator and the necessary service."""

import os
import runpy
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_legacy_quickstart_credentials_are_restricted_without_changing_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ["unseal.key", "root.token", "admin-token", "client.key", "bundle.key", "server.key"]
    for name in names:
        (tmp_path / name).write_text("sensitive")
        (tmp_path / name).chmod(0o644)
    owners: dict[str, tuple[int, int]] = {}
    monkeypatch.setattr(
        os, "chown", lambda path, uid, gid: owners.update({Path(path).name: (uid, gid)})
    )
    secure = runpy.run_path(str(ROOT / "deploy/quickstart/secure-secrets.py"))["secure"]
    secure(tmp_path, 1000, 1000)
    for name in names:
        expected_group = (
            10001 if name in {"client.key", "bundle.key"} else 101 if name == "server.key" else 1000
        )
        expected_mode = 0o600 if name in {"unseal.key", "root.token", "admin-token"} else 0o640
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == expected_mode
        assert owners[name] == (1000, expected_group)
        assert (tmp_path / name).read_text() == "sensitive"


def test_credential_repair_refuses_symlinks(tmp_path: Path) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("leave this file alone")
    unrelated.chmod(0o644)
    (tmp_path / "unseal.key").symlink_to(unrelated)
    secure = runpy.run_path(str(ROOT / "deploy/quickstart/secure-secrets.py"))["secure"]
    with pytest.raises(ValueError, match="symlink"):
        secure(tmp_path, 1000, 1000)
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o644
    assert unrelated.read_text() == "leave this file alone"

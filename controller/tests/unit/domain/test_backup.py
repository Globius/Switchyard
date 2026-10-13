# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for backup archives and target settings (ADR 0205)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from switchyard.domain.backup import (
    BackupError,
    BackupTargetConfig,
    archive_name,
    decrypt,
    encrypt,
    is_archive_name,
    prune,
)

SALT, NONCE = b"s" * 16, b"n" * 12


def test_round_trip_and_wrong_passphrase() -> None:
    blob = encrypt(b"hello" * 1000, "pw", salt=SALT, nonce=NONCE)
    assert b"hello" not in blob
    assert decrypt(blob, "pw") == b"hello" * 1000
    with pytest.raises(BackupError):
        decrypt(blob, "other")


def test_damage_and_foreign_files_are_refused() -> None:
    blob = bytearray(encrypt(b"x", "pw", salt=SALT, nonce=NONCE))
    blob[-1] ^= 1
    with pytest.raises(BackupError):
        decrypt(bytes(blob), "pw")
    with pytest.raises(BackupError):
        decrypt(b"not an archive at all, just some text", "pw")
    with pytest.raises(ValueError):
        encrypt(b"x", "pw", salt=b"short", nonce=NONCE)


def test_names_and_pruning() -> None:
    when = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
    assert archive_name("0.1.0", when) == "switchyard-0.1.0-20261013T040000Z.syb"
    assert is_archive_name(archive_name("0.1.0", when))
    assert not is_archive_name("../etc/passwd") and not is_archive_name("a.syb")
    names = [archive_name("0.1.0", datetime(2026, 10, d, tzinfo=UTC)) for d in (3, 1, 2)]
    assert prune([*names, "keep-me.txt"], 2) == [names[1]]
    assert prune(names, 5) == []


def test_target_settings_are_validated() -> None:
    s3 = {
        "kind": "s3",
        "endpoint": "https://s3.example.test",
        "bucket": "sy-backups",
        "access_key_ref": "secret/b/ak",
        "secret_key_ref": "secret/b/sk",
    }
    ok = {"name": "nightly", "passphrase_ref": "secret/backup/pass", "target": s3}
    assert BackupTargetConfig.model_validate(ok).keep == 14
    bad_cases = (
        {**ok, "name": "../x"},
        {**ok, "target": {**s3, "endpoint": "http://plain.example.test"}},
        {**ok, "target": {"kind": "ftp", "host": "h", "username": "u", "password_ref": "p; rm"}},
        {**ok, "target": {"kind": "local", "path": "a b"}},
        {**ok, "extra": 1},
    )
    for bad in bad_cases:
        with pytest.raises(ValidationError):
            BackupTargetConfig.model_validate(bad)

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Backup stores: local directory, S3 (against a fake server) and name safety (ADR 0205)."""

import datetime as dt
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from switchyard.adapters.backup_targets import (
    BackupTargetError,
    FtpStore,
    LocalStore,
    MemoryStore,
    S3Store,
    SftpStore,
    build_store,
)
from switchyard.adapters.dns.route53 import AwsCredentials
from switchyard.adapters.secrets import MemorySecrets
from switchyard.domain.backup import BackupTargetConfig, FtpTarget, S3Target

A = "switchyard-0.1.0-20261001T000000Z.syb"
B = "switchyard-0.1.0-20261002T000000Z.syb"
NOW = dt.datetime(2026, 10, 13, 4, 0, tzinfo=dt.UTC)
TARGET = S3Target(
    endpoint="https://s3.example.test",
    bucket="sybk",
    prefix="nightly",
    access_key_ref="r/ak",
    secret_key_ref="r/sk",
)


@pytest.mark.parametrize("make", ["memory", "local"])
async def test_put_get_list_delete(make: str, tmp_path: Path) -> None:
    store = MemoryStore() if make == "memory" else LocalStore(tmp_path, "b")
    await store.put(B, b"two")
    await store.put(A, b"one")
    assert await store.list() == [A, B]
    assert await store.get(A) == b"one"
    await store.delete(A)
    await store.delete(A)  # missing is fine
    assert await store.list() == [B]
    with pytest.raises(BackupTargetError):
        await store.get(A)


@pytest.mark.parametrize("make", ["memory", "local"])
async def test_only_archive_names_are_accepted(make: str, tmp_path: Path) -> None:
    store = MemoryStore() if make == "memory" else LocalStore(tmp_path)
    for bad in ("../x.syb", "notes.txt", "/etc/passwd", "a/b.syb"):
        with pytest.raises(BackupTargetError):
            await store.put(bad, b"x")
        with pytest.raises(BackupTargetError):
            await store.get(bad)


def test_local_path_cannot_leave_the_root(tmp_path: Path) -> None:
    with pytest.raises(BackupTargetError):
        LocalStore(tmp_path, "../elsewhere")


async def test_local_ignores_other_files(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("mine")
    store = LocalStore(tmp_path)
    await store.put(A, b"x")
    assert await store.list() == [A]
    assert (tmp_path / "keep.txt").read_text() == "mine"


@pytest.mark.parametrize("name", ["../private.syb", "/etc/passwd", A + "\n", A + "\r\nDELE x"])
async def test_ftp_refuses_invalid_names_before_connecting(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FtpStore(
        FtpTarget(host="ftp.example.test", username="backup", password_ref="r/p"), "pw"
    )

    def unexpected_connection() -> None:
        pytest.fail("an invalid archive name must be refused before connecting")

    monkeypatch.setattr(store, "_connect", unexpected_connection)
    with pytest.raises(BackupTargetError, match="not a backup archive name"):
        await store.put(name, b"backup")
    with pytest.raises(BackupTargetError, match="not a backup archive name"):
        await store.get(name)
    with pytest.raises(BackupTargetError, match="not a backup archive name"):
        await store.delete(name)


async def test_local_backup_does_not_follow_a_planted_temporary_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "unrelated"
    victim.write_bytes(b"preserve me")
    root = tmp_path / "backups"
    root.mkdir()
    (root / A).with_suffix(".part").symlink_to(victim)
    store = LocalStore(root)
    await store.put(A, b"encrypted archive")
    assert victim.read_bytes() == b"preserve me"
    assert await store.get(A) == b"encrypted archive"
    assert (root / A).stat().st_mode & 0o077 == 0


def fake_s3() -> tuple[httpx.AsyncClient, dict[str, bytes], list[httpx.Request]]:
    objects: dict[str, bytes] = {}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = unquote(request.url.path)
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/")
        assert request.headers["x-amz-content-sha256"]
        if request.method == "GET" and path == "/sybk":
            keys = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in sorted(objects))
            body = (
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>false</IsTruncated>{keys}</ListBucketResult>"
            )
            return httpx.Response(200, text=body)
        key = path.removeprefix("/sybk/")
        if request.method == "PUT":
            objects[key] = request.content
            return httpx.Response(200)
        if request.method == "GET":
            if key in objects:
                return httpx.Response(200, content=objects[key])
            return httpx.Response(404)
        if request.method == "DELETE":
            objects.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(405)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), objects, seen


async def test_s3_round_trip_with_signed_requests() -> None:
    client, objects, seen = fake_s3()
    store = S3Store(TARGET, AwsCredentials("AK", "SK"), client=client, now=lambda: NOW)
    await store.put(A, b"one")
    await store.put(B, b"two")
    assert set(objects) == {f"nightly/{A}", f"nightly/{B}"}
    assert await store.list() == [A, B]
    assert await store.get(B) == b"two"
    await store.delete(A)
    assert await store.list() == [B]
    assert all("20261013T040000Z" in r.headers["x-amz-date"] for r in seen)
    with pytest.raises(BackupTargetError):
        await store.get(A)


async def test_s3_errors_are_reported_without_the_response_body() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(403, text="SECRET-DETAIL"))
    )
    store = S3Store(TARGET, AwsCredentials("AK", "SK"), client=client, now=lambda: NOW)
    with pytest.raises(BackupTargetError, match="HTTP 403") as err:
        await store.put(A, b"x")
    assert "SECRET-DETAIL" not in str(err.value)


async def test_build_store_reads_credentials_from_secrets(tmp_path: Path) -> None:
    config = BackupTargetConfig(name="n", passphrase_ref="r/p", target=TARGET)
    secrets = MemorySecrets({"r/ak": "AK", "r/sk": "SK"})
    assert isinstance(await build_store(config, secrets, tmp_path), S3Store)
    local = BackupTargetConfig.model_validate(
        {"name": "l", "passphrase_ref": "r/p", "target": {"kind": "local", "path": "x"}}
    )
    assert isinstance(await build_store(local, secrets, tmp_path), LocalStore)
    sftp = BackupTargetConfig.model_validate(
        {
            "name": "s",
            "passphrase_ref": "r/p",
            "target": {
                "kind": "sftp",
                "host": "h.example.test",
                "username": "u",
                "host_key_sha256": "SHA256:" + "A" * 43,
                "key_ref": "r/k",
            },
        }
    )
    assert isinstance(await build_store(sftp, MemorySecrets({"r/k": "pem"}), tmp_path), SftpStore)


async def test_sftp_against_a_real_server_with_a_pinned_host_key(tmp_path: Path) -> None:
    import asyncssh

    from switchyard.domain.backup import SftpTarget

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    authorized = tmp_path / "authorized_keys"
    authorized.write_bytes(client_key.export_public_key())
    root = tmp_path / "srv"
    root.mkdir()

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def public_key_auth_supported(self) -> bool:
            return True

        def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
            return bool(key.export_public_key() == client_key.export_public_key())

    acceptor = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=Server,
        server_host_keys=[host_key],
        sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root).encode()),
    )
    port = acceptor.get_port()
    pem = client_key.export_private_key().decode()

    def target(fingerprint: str) -> SftpTarget:
        return SftpTarget(
            host="127.0.0.1",
            port=port,
            username="bk",
            host_key_sha256=fingerprint,
            key_ref="r/k",
            directory="backups",
        )

    try:
        store = SftpStore(target(host_key.get_fingerprint("sha256")), pem)
        assert await store.list() == []
        await store.put(B, b"two")
        await store.put(A, b"one")
        assert await store.list() == [A, B]
        assert await store.get(A) == b"one"
        await store.delete(A)
        await store.delete(A)
        assert await store.list() == [B]
        with pytest.raises(BackupTargetError):
            await store.get(A)
        wrong = SftpStore(target("SHA256:" + "A" * 43), pem)
        with pytest.raises(BackupTargetError) as refused:
            await wrong.list()  # an unpinned host key is refused before login
        assert "host key" in str(refused.value).lower()
    finally:
        acceptor.close()
        await acceptor.wait_closed()


@pytest.mark.parametrize("key_type", ["ssh-rsa", "ecdsa-sha2-nistp256", "ssh-ed25519"])
async def test_sftp_pins_a_host_key_of_any_type_the_server_offers(
    tmp_path: Path, key_type: str
) -> None:
    """A server with several host keys: pinning any one of them works, not just the first it offers."""
    import asyncssh

    from switchyard.domain.backup import SftpTarget

    keys = {
        "ssh-rsa": asyncssh.generate_private_key("ssh-rsa", key_size=2048),
        "ecdsa-sha2-nistp256": asyncssh.generate_private_key("ecdsa-sha2-nistp256"),
        "ssh-ed25519": asyncssh.generate_private_key("ssh-ed25519"),
    }
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    root = tmp_path / "srv"
    root.mkdir()

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def public_key_auth_supported(self) -> bool:
            return True

        def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
            return bool(key.export_public_key() == client_key.export_public_key())

    acceptor = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=Server,
        server_host_keys=[keys["ssh-rsa"], keys["ecdsa-sha2-nistp256"], keys["ssh-ed25519"]],
        sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root).encode()),
    )
    try:
        target = SftpTarget(
            host="127.0.0.1",
            port=acceptor.get_port(),
            username="bk",
            host_key_sha256=keys[key_type].get_fingerprint("sha256"),
            key_ref="r/k",
        )
        store = SftpStore(target, client_key.export_private_key().decode())
        assert await store.list() == []
    finally:
        acceptor.close()
        await acceptor.wait_closed()

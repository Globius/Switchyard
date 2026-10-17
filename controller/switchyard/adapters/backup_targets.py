# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Where configuration backups are written: local disk, S3-compatible storage, FTP/FTPS (ADR 0205).

Every store holds opaque, already-encrypted archives under names from
:func:`switchyard.domain.backup.archive_name`; stores refuse any other name, so a target
setting can never make the controller read or overwrite an unrelated file. Credentials are
resolved from ``secret_ref`` values at use time. :class:`MemoryStore` is the fake for tests.
"""

import asyncio
import datetime as dt
import ftplib
import io
import os
import ssl
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

import asyncssh
import httpx
from defusedxml import ElementTree as SafeET

from switchyard.adapters.dns.route53 import AwsCredentials, sign
from switchyard.adapters.secrets import SecretsReader
from switchyard.domain.backup import (
    BackupTargetConfig,
    FtpTarget,
    LocalTarget,
    S3Target,
    SftpTarget,
    is_archive_name,
)


class BackupTargetError(Exception):
    """The target could not be reached, refused the request, or is not supported."""


class BackupStore(Protocol):
    """Reads and writes archives at one destination."""

    async def put(self, name: str, data: bytes) -> None:
        """Write an archive, replacing one of the same name."""
        ...

    async def get(self, name: str) -> bytes:
        """Read an archive. Raises :class:`BackupTargetError` if it is missing."""
        ...

    async def list(self) -> list[str]:
        """Names of the archives present (other files are not listed), oldest first."""
        ...

    async def delete(self, name: str) -> None:
        """Remove an archive; missing is not an error."""
        ...


def _checked(name: str) -> str:
    if not is_archive_name(name):
        raise BackupTargetError(f"{name!r} is not a backup archive name")
    return name


class MemoryStore:
    """In-memory store, for tests."""

    def __init__(self) -> None:
        """Start empty."""
        self.files: dict[str, bytes] = {}

    async def put(self, name: str, data: bytes) -> None:
        """Store ``data``."""
        self.files[_checked(name)] = data

    async def get(self, name: str) -> bytes:
        """Return the archive."""
        try:
            return self.files[_checked(name)]
        except KeyError as exc:
            raise BackupTargetError(f"{name} not found") from exc

    async def list(self) -> list[str]:
        """Archive names, oldest first."""
        return sorted((n for n in self.files if is_archive_name(n)), key=lambda n: n[-17:])

    async def delete(self, name: str) -> None:
        """Remove the archive."""
        self.files.pop(_checked(name), None)


class LocalStore:
    """A directory under the configured backup root. Written atomically (temp file, then rename)."""

    def __init__(self, root: Path, sub: str = "") -> None:
        """``sub`` is relative to ``root``; a path that leaves ``root`` is refused."""
        base = (root / sub).resolve()
        if root.resolve() not in (base, *base.parents):
            raise BackupTargetError("the backup path leaves the backup root")
        self._dir = base

    async def put(self, name: str, data: bytes) -> None:
        """Write the archive."""
        path = self._dir / _checked(name)

        def write() -> None:
            self._dir.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self._dir, suffix=".part")
            tmp = Path(temporary)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)

        await asyncio.to_thread(write)

    async def get(self, name: str) -> bytes:
        """Read the archive."""
        path = self._dir / _checked(name)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise BackupTargetError(f"{name} not found") from exc

    async def list(self) -> list[str]:
        """Archive names, oldest first."""
        if not self._dir.is_dir():
            return []
        names = await asyncio.to_thread(lambda: [p.name for p in self._dir.iterdir()])
        return sorted((n for n in names if is_archive_name(n)), key=lambda n: n[-17:])

    async def delete(self, name: str) -> None:
        """Remove the archive."""
        await asyncio.to_thread((self._dir / _checked(name)).unlink, True)


class S3Store:
    """S3-compatible object storage over HTTPS, path-style, Signature Version 4."""

    def __init__(
        self,
        target: S3Target,
        credentials: AwsCredentials,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        """``client`` is injectable for tests."""
        self._t, self._creds = target, credentials
        self._client = client or httpx.AsyncClient(timeout=60.0)
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self._host = target.endpoint.removeprefix("https://")

    def _key(self, name: str) -> str:
        prefix = self._t.prefix.strip("/")
        return f"{prefix}/{name}" if prefix else name

    async def _call(
        self, method: str, key: str, body: bytes = b"", query: dict[str, str] | None = None
    ) -> httpx.Response:
        path = f"/{self._t.bucket}/{key}" if key else f"/{self._t.bucket}"
        q = query or {}
        headers = sign(
            method, self._host, path, q, body, self._creds,
            service="s3", now=self._now(), region=self._t.region, s3=True,
        )  # fmt: skip
        try:
            res = await self._client.request(
                method, f"{self._t.endpoint}{path}", params=q, content=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise BackupTargetError(f"S3 request failed: {type(exc).__name__}") from exc
        return res

    async def put(self, name: str, data: bytes) -> None:
        """Upload the archive."""
        res = await self._call("PUT", self._key(_checked(name)), data)
        if res.status_code // 100 != 2:
            raise BackupTargetError(f"S3 refused the upload (HTTP {res.status_code})")

    async def get(self, name: str) -> bytes:
        """Download the archive."""
        res = await self._call("GET", self._key(_checked(name)))
        if res.status_code == 404:
            raise BackupTargetError(f"{name} not found")
        if res.status_code // 100 != 2:
            raise BackupTargetError(f"S3 refused the download (HTTP {res.status_code})")
        return res.content

    async def list(self) -> list[str]:
        """Archive names under the prefix, oldest first (follows continuation tokens)."""
        names: list[str] = []
        token = ""
        while True:
            query = {"list-type": "2", "prefix": self._t.prefix.strip("/")}
            if token:
                query["continuation-token"] = token
            res = await self._call("GET", "", query=query)
            if res.status_code // 100 != 2:
                raise BackupTargetError(f"S3 refused the listing (HTTP {res.status_code})")
            root = SafeET.fromstring(res.text)
            ns = root.tag.partition("}")[0] + "}" if root.tag.startswith("{") else ""
            names += [PurePosixPath(k.text or "").name for k in root.iter(f"{ns}Key")]
            token = (
                (root.findtext(f"{ns}NextContinuationToken") or "")
                if (root.findtext(f"{ns}IsTruncated") == "true")
                else ""
            )
            if not token:
                break
        return sorted((n for n in names if is_archive_name(n)), key=lambda n: n[-17:])

    async def delete(self, name: str) -> None:
        """Remove the archive."""
        res = await self._call("DELETE", self._key(_checked(name)))
        if res.status_code not in (200, 204, 404):
            raise BackupTargetError(f"S3 refused the delete (HTTP {res.status_code})")


class FtpStore:
    """FTP or explicit FTPS (certificate and host name verified), run in a worker thread."""

    def __init__(self, target: FtpTarget, password: str, timeout: float = 30.0) -> None:
        """Create the store; nothing connects until the first call."""
        self._t, self._password, self._timeout = target, password, timeout

    def _connect(self) -> ftplib.FTP:
        t = self._t
        ftp: ftplib.FTP
        if t.tls:
            ftp = ftplib.FTP_TLS(  # noqa: S321 (explicit FTPS, verified)  # nosemgrep: python-lang-crypto-ftplib-atomic
                context=ssl.create_default_context(), timeout=self._timeout
            )
        else:
            ftp = ftplib.FTP(timeout=self._timeout)  # noqa: S321 (operator chose plain FTP)  # nosemgrep: python-lang-crypto-ftplib-atomic
        ftp.connect(t.host, t.port)
        ftp.login(t.username, self._password)
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        if t.directory:
            ftp.cwd(t.directory)
        return ftp

    async def put(self, name: str, data: bytes) -> None:
        """Upload via a temporary name, then rename."""
        _checked(name)

        def work() -> None:
            ftp = self._connect()
            try:
                ftp.storbinary(f"STOR {name}.part", io.BytesIO(data))
                ftp.rename(f"{name}.part", name)
            finally:
                ftp.close()

        await self._guard(work)

    async def get(self, name: str) -> bytes:
        """Download the archive."""
        _checked(name)
        buf = io.BytesIO()

        def work() -> None:
            ftp = self._connect()
            try:
                ftp.retrbinary(f"RETR {name}", buf.write)
            finally:
                ftp.close()

        await self._guard(work)
        return buf.getvalue()

    async def list(self) -> list[str]:
        """Archive names, oldest first."""
        found: list[str] = []

        def work() -> None:
            ftp = self._connect()
            try:
                found.extend(PurePosixPath(n).name for n in ftp.nlst())
            finally:
                ftp.close()

        await self._guard(work)
        return sorted((n for n in found if is_archive_name(n)), key=lambda n: n[-17:])

    async def delete(self, name: str) -> None:
        """Remove the archive."""
        _checked(name)

        def work() -> None:
            ftp = self._connect()
            try:
                ftp.delete(name)
            except ftplib.error_perm:
                pass
            finally:
                ftp.close()

        await self._guard(work)

    async def _guard(self, work: Callable[[], None]) -> None:
        try:
            await asyncio.to_thread(work)
        except (OSError, ftplib.Error, ssl.SSLError) as exc:
            raise BackupTargetError(f"FTP failed: {type(exc).__name__}: {exc}") from exc


class _PinnedKeyClient(asyncssh.SSHClient):
    """Accepts exactly one server host key, identified by its SHA-256 fingerprint."""

    def __init__(self, fingerprint: str) -> None:
        self._fingerprint = fingerprint

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        """True only for the pinned key; there is no trust on first use."""
        return bool(key.get_fingerprint("sha256") == self._fingerprint)


class SftpStore:
    """SFTP over SSH with a pinned host key and public-key login (asyncssh)."""

    def __init__(self, target: SftpTarget, private_key_pem: str, timeout: float = 30.0) -> None:
        """Create the store; nothing connects until the first call."""
        self._t, self._key_pem, self._timeout = target, private_key_pem, timeout

    #: Host key algorithm families, tried in turn. The server presents the first family it shares
    #: with us, so a fingerprint of another key type than that one would never match; each family
    #: is tried until the pinned key is the one offered.
    _HOST_KEY_FAMILIES: tuple[tuple[str, ...], ...] = (
        ("ssh-ed25519",),
        ("ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"),
        ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"),
    )

    async def _session(self) -> tuple[asyncssh.SSHClientConnection, asyncssh.SFTPClient]:
        t = self._t
        try:
            key = asyncssh.import_private_key(self._key_pem)
            refused: asyncssh.Error | None = None
            for family in self._HOST_KEY_FAMILIES:
                try:
                    conn, _ = await asyncio.wait_for(
                        asyncssh.create_connection(
                            lambda: _PinnedKeyClient(t.host_key_sha256),
                            t.host,
                            t.port,
                            username=t.username,
                            client_keys=[key],
                            known_hosts=(),
                            password=None,
                            agent_path=None,
                            server_host_key_algs=list(family),
                        ),
                        self._timeout,
                    )
                except (asyncssh.HostKeyNotVerifiable, asyncssh.KeyExchangeFailed) as exc:
                    refused = exc  # not this key type; try the next family
                    continue
                return conn, await conn.start_sftp_client()
            raise refused or asyncssh.HostKeyNotVerifiable("no host key family matched")
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            raise BackupTargetError(f"SFTP failed: {type(exc).__name__}: {exc}") from exc

    def _path(self, name: str = "") -> str:
        base = self._t.directory.strip("/")
        return f"{base}/{name}" if base and name else (base or name or ".")

    async def _do[T](self, op: Callable[[asyncssh.SFTPClient], Awaitable[T]]) -> T:
        conn, sftp = await self._session()
        try:
            return await op(sftp)
        except asyncssh.SFTPNoSuchFile as exc:
            raise BackupTargetError("not found") from exc
        except (asyncssh.Error, OSError) as exc:
            raise BackupTargetError(f"SFTP failed: {type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()

    async def put(self, name: str, data: bytes) -> None:
        """Upload via a temporary name, then rename."""
        _checked(name)

        async def op(sftp: asyncssh.SFTPClient) -> None:
            if self._t.directory:
                await sftp.makedirs(self._path(), exist_ok=True)
            tmp = self._path(f"{name}.part")
            async with sftp.open(tmp, "wb") as fh:
                await fh.write(data)
            await sftp.posix_rename(tmp, self._path(name))

        await self._do(op)

    async def get(self, name: str) -> bytes:
        """Download the archive."""
        _checked(name)

        async def op(sftp: asyncssh.SFTPClient) -> bytes:
            async with sftp.open(self._path(name), "rb") as fh:
                return cast("bytes", await fh.read())

        try:
            return await self._do(op)
        except BackupTargetError as exc:
            if str(exc) == "not found":
                raise BackupTargetError(f"{name} not found") from exc
            raise

    async def list(self) -> list[str]:
        """Archive names, oldest first; a missing directory is an empty list."""

        async def op(sftp: asyncssh.SFTPClient) -> list[str]:
            return [str(n) for n in await sftp.listdir(self._path())]

        try:
            names = await self._do(op)
        except BackupTargetError as exc:
            if str(exc) == "not found":
                return []
            raise
        return sorted((n for n in names if is_archive_name(n)), key=lambda n: n[-17:])

    async def delete(self, name: str) -> None:
        """Remove the archive; missing is not an error."""
        _checked(name)

        async def op(sftp: asyncssh.SFTPClient) -> None:
            await sftp.remove(self._path(name))

        try:
            await self._do(op)
        except BackupTargetError as exc:
            if str(exc) != "not found":
                raise


async def build_store(
    config: BackupTargetConfig, secrets: SecretsReader, local_root: Path
) -> BackupStore:
    """Open the store for ``config``, reading its credentials from ``secrets``.

    Raises:
        BackupTargetError: Never; opening is lazy (errors surface on first use).
    """
    t = config.target
    if isinstance(t, LocalTarget):
        return LocalStore(local_root, t.path)
    if isinstance(t, S3Target):
        creds = AwsCredentials(
            await secrets.get(t.access_key_ref), await secrets.get(t.secret_key_ref)
        )
        return S3Store(t, creds)
    if isinstance(t, FtpTarget):
        return FtpStore(t, await secrets.get(t.password_ref))
    if isinstance(t, SftpTarget):
        return SftpStore(t, await secrets.get(t.key_ref))
    raise BackupTargetError("unknown target kind")  # pragma: no cover

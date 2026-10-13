# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Configuration backup archives: naming, encryption and target settings (ADR 0205).

Pure functions and models, no I/O. An archive is ``MAGIC | salt | nonce | AES-256-GCM(ciphertext)``
with a key derived from a passphrase by scrypt. The plaintext is gzip-compressed JSON. The
passphrase is a secret value resolved from a ``secret_ref``; it is never stored with the archive.

Example:
    >>> blob = encrypt(b'{"a": 1}', "correct horse", salt=b"s" * 16, nonce=b"n" * 12)
    >>> decrypt(blob, "correct horse")
    b'{"a": 1}'
    >>> archive_name("0.1.0", datetime(2026, 10, 13, 4, 0, tzinfo=UTC))
    'switchyard-0.1.0-20261013T040000Z.syb'
"""

import gzip
import io
import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from pydantic import BaseModel, ConfigDict, Field

MAGIC = b"SYB1"
_SALT, _NONCE = 16, 12
#: Archives are refused above this many bytes when decompressing (a zip-bomb guard).
MAX_PLAINTEXT = 512 * 1024 * 1024

_NAME = re.compile(r"^switchyard-[0-9A-Za-z.+-]{1,32}-\d{8}T\d{6}Z\.syb$")
_SAFE_NAME = r"^[a-z0-9][a-z0-9-]{0,62}$"
_HOST = r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
_BUCKET = r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
_PREFIX = r"^[A-Za-z0-9._/-]{0,200}$"
_REF = r"^[A-Za-z0-9._/:-]{1,200}$"


class BackupError(Exception):
    """An archive could not be read: wrong passphrase, damaged or not a Switchyard archive."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalTarget(_Strict):
    """A directory on the controller's disk. ``path`` is under the configured backup root."""

    kind: Literal["local"] = "local"
    path: Annotated[str, Field(pattern=_PREFIX)] = ""


class S3Target(_Strict):
    """An S3-compatible bucket (AWS, MinIO, Ceph, Backblaze B2, ...). Path-style addressing."""

    kind: Literal["s3"] = "s3"
    endpoint: Annotated[str, Field(pattern=r"^https://[A-Za-z0-9.-]+(:\d{1,5})?$")]
    region: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,32}$")] = "us-east-1"
    bucket: Annotated[str, Field(pattern=_BUCKET)]
    prefix: Annotated[str, Field(pattern=_PREFIX)] = ""
    access_key_ref: Annotated[str, Field(pattern=_REF)]
    secret_key_ref: Annotated[str, Field(pattern=_REF)]


class FtpTarget(_Strict):
    """An FTP server. ``tls`` uses explicit FTPS; plain FTP sends the password in clear."""

    kind: Literal["ftp"] = "ftp"
    host: Annotated[str, Field(pattern=_HOST)]
    port: Annotated[int, Field(ge=1, le=65535)] = 21
    tls: bool = True
    username: Annotated[str, Field(pattern=r"^[A-Za-z0-9._@-]{1,64}$")]
    password_ref: Annotated[str, Field(pattern=_REF)]
    directory: Annotated[str, Field(pattern=_PREFIX)] = ""


class SftpTarget(_Strict):
    """A server reached over SSH (SFTP). The host key is pinned; there is no trust on first use."""

    kind: Literal["sftp"] = "sftp"
    host: Annotated[str, Field(pattern=_HOST)]
    port: Annotated[int, Field(ge=1, le=65535)] = 22
    username: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")]
    host_key_sha256: Annotated[str, Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")]
    key_ref: Annotated[str, Field(pattern=_REF)]
    directory: Annotated[str, Field(pattern=_PREFIX)] = ""


Target = Annotated[LocalTarget | S3Target | FtpTarget | SftpTarget, Field(discriminator="kind")]


class BackupTargetConfig(_Strict):
    """A named backup destination plus the secret that encrypts archives sent to it."""

    name: Annotated[str, Field(pattern=_SAFE_NAME)]
    target: Target
    passphrase_ref: Annotated[str, Field(pattern=_REF)]
    keep: Annotated[int, Field(ge=1, le=1000)] = 14


def archive_name(version: str, when: datetime) -> str:
    """The file name of an archive made by controller ``version`` at ``when`` (UTC)."""
    stamp = when.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"switchyard-{version}-{stamp}.syb"


def is_archive_name(name: str) -> bool:
    """Whether ``name`` is one this module would produce (targets list and prune only these)."""
    return _NAME.match(name) is not None


def _stamp(name: str) -> str:
    return name.rsplit("-", 1)[1]


def prune(names: list[str], keep: int) -> list[str]:
    """The archives to delete so that only the newest ``keep`` remain. Other files are ignored.

    Example:
        >>> a = "switchyard-0.1.0-20261001T000000Z.syb"
        >>> b = "switchyard-0.1.0-20261002T000000Z.syb"
        >>> prune([a, b, "notes.txt"], 1) == [a]
        True
    """
    mine = sorted((n for n in names if is_archive_name(n)), key=_stamp)
    return mine[: max(0, len(mine) - keep)]


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())


def encrypt(plain: bytes, passphrase: str, *, salt: bytes, nonce: bytes) -> bytes:
    """Compress and encrypt `plain`; `salt` (16 bytes) and `nonce` (12) are random per archive."""
    if len(salt) != _SALT or len(nonce) != _NONCE:
        raise ValueError("salt must be 16 bytes and nonce 12 bytes")
    sealed = AESGCM(_key(passphrase, salt)).encrypt(nonce, gzip.compress(plain, mtime=0), MAGIC)
    return MAGIC + salt + nonce + sealed


def decrypt(blob: bytes, passphrase: str) -> bytes:
    """Decrypt and decompress an archive.

    Raises:
        BackupError: Not an archive, damaged, or the passphrase is wrong.
    """
    head = len(MAGIC) + _SALT + _NONCE
    if len(blob) <= head + 16 or not blob.startswith(MAGIC):
        raise BackupError("not a Switchyard backup archive")
    salt, nonce = blob[len(MAGIC) : len(MAGIC) + _SALT], blob[len(MAGIC) + _SALT : head]
    try:
        packed = AESGCM(_key(passphrase, salt)).decrypt(nonce, blob[head:], MAGIC)
    except InvalidTag as exc:
        raise BackupError("wrong passphrase or damaged archive") from exc
    with gzip.GzipFile(fileobj=io.BytesIO(packed)) as gz:
        plain = gz.read(MAX_PLAINTEXT + 1)
    if len(plain) > MAX_PLAINTEXT:
        raise BackupError("archive expands beyond the size limit")
    return plain

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""UUIDv7 identifiers.

All object ids are UUIDv7 so they sort by creation time (backend design, "Tenancy and edits").
Python 3.12 has no ``uuid.uuid7``, so this module implements RFC 9562 section 5.7.
"""

import os
import time
import uuid


def uuid7(*, timestamp_ms: int | None = None, random_bytes: bytes | None = None) -> uuid.UUID:
    """Return a new UUIDv7.

    Layout (RFC 9562): 48-bit Unix millisecond timestamp, 4-bit version, 12 random bits,
    2-bit variant, 62 random bits.

    Args:
        timestamp_ms: Unix time in milliseconds. Defaults to the current time. Tests pass it to
            get deterministic ordering.
        random_bytes: Exactly 10 random bytes. Defaults to ``os.urandom(10)``.

    Returns:
        A UUID whose ``version`` is 7.

    Example:
        >>> u = uuid7(timestamp_ms=0x0123456789AB, random_bytes=bytes(10))
        >>> u.version
        7
        >>> str(u)[:13]
        '01234567-89ab'
    """
    ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if not 0 <= ms < 1 << 48:
        raise ValueError("timestamp_ms must fit in 48 bits")
    rand = os.urandom(10) if random_bytes is None else random_bytes
    if len(rand) != 10:
        raise ValueError("random_bytes must be exactly 10 bytes")
    value = ms << 80
    value |= 0x7 << 76
    value |= (int.from_bytes(rand[:2], "big") & 0x0FFF) << 64
    value |= 0b10 << 62
    value |= int.from_bytes(rand[2:], "big") & ((1 << 62) - 1)
    return uuid.UUID(int=value)

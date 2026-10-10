"""Tests for UUIDv7 generation."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from switchyard.ids import uuid7


@given(st.integers(min_value=0, max_value=2**48 - 1), st.binary(min_size=10, max_size=10))
def test_layout(ms: int, rand: bytes) -> None:
    u = uuid7(timestamp_ms=ms, random_bytes=rand)
    assert u.version == 7
    assert u.variant == "specified in RFC 4122"
    assert u.int >> 80 == ms


@given(
    st.integers(min_value=0, max_value=2**48 - 2),
    st.binary(min_size=10, max_size=10),
    st.binary(min_size=10, max_size=10),
)
def test_sorts_by_timestamp(ms: int, a: bytes, b: bytes) -> None:
    assert uuid7(timestamp_ms=ms, random_bytes=a) < uuid7(timestamp_ms=ms + 1, random_bytes=b)


def test_defaults_are_unique() -> None:
    assert len({uuid7() for _ in range(1000)}) == 1000


def test_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="48 bits"):
        uuid7(timestamp_ms=2**48)
    with pytest.raises(ValueError, match="10 bytes"):
        uuid7(random_bytes=b"short")

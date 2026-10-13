# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the renderer's second-line escaping and token checks."""

import ipaddress

import pytest
from hypothesis import given
from hypothesis import strategies as st

from switchyard.render import escape as esc
from switchyard.render.escape import RenderError

HAZARDS = [
    " ",
    "\t",
    "\n",
    "\r",
    "#",
    '"',
    "'",
    "\\",
    "$",
    "{",
    "}",
    "(",
    ")",
    "[",
    "]",
    "|",
    "&",
    "<",
    ">",
    "`",
    "\x00",
]


@pytest.mark.parametrize("fn", [esc.ident, esc.arg, esc.hostname, esc.header_name])
@pytest.mark.parametrize("hazard", HAZARDS)
def test_tokens_reject_every_hazard_character(fn: object, hazard: str) -> None:
    assert callable(fn)
    with pytest.raises(RenderError):
        fn(f"a{hazard}b")


@pytest.mark.parametrize("hazard", [h for h in HAZARDS if h not in {"&"}])
def test_location_rejects_hazards(hazard: str) -> None:
    with pytest.raises(RenderError):
        esc.location(f"/a{hazard}b")


@given(st.text(max_size=30))
def test_arg_accepts_only_the_safe_alphabet(value: str) -> None:
    try:
        out = esc.arg(value)
    except RenderError:
        return
    assert out == value
    assert all((c.isalnum() and c.isascii()) or c in "._~/%:@+,;=*-" for c in out)


@given(st.text(max_size=30))
def test_location_never_leaves_a_single_percent(value: str) -> None:
    try:
        out = esc.location(value)
    except RenderError:
        return
    assert "%" not in out.replace("%%", "")


def test_ident_rejects_empty_and_edge_punctuation() -> None:
    for bad in ["", "-a", "a-", ".a", "A"]:
        with pytest.raises(RenderError):
            esc.ident(bad)


def test_wildcard_regex_has_no_backslash_and_is_anchored() -> None:
    regex = esc.wildcard_regex("*.a-b.example.test")
    assert regex == "^[^.]+[.]a-b[.]example[.]test$"
    assert "\\" not in regex


def test_wildcard_regex_requires_wildcard() -> None:
    with pytest.raises(RenderError):
        esc.wildcard_regex("a.example.test")


def test_bind_address_forms() -> None:
    assert esc.bind_address(ipaddress.ip_address("192.0.2.1"), 80) == "192.0.2.1:80"
    assert esc.bind_address(ipaddress.ip_address("2001:db8::1"), 80) == "ipv6@2001:db8::1:80"
    with pytest.raises(RenderError):
        esc.bind_address(ipaddress.ip_address("192.0.2.1"), 0)

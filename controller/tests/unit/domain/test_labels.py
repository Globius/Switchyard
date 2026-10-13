# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Object labels and the ``?label=`` selectors (ADR 0198)."""

import uuid

import pytest
from pydantic import ValidationError

from switchyard.domain.models import Pool
from switchyard.domain.validators import labels_match, parse_label_selector, validate_labels


def _pool(**kw: object) -> Pool:
    return Pool(id=uuid.UUID(int=1), tenant_id=uuid.UUID(int=2), name="web", **kw)


def test_labels_default_to_empty_and_accept_valid_pairs() -> None:
    assert _pool().labels == {}
    assert _pool(labels={"env": "prod", "team.name": "pay-ments", "flag": ""}).labels["flag"] == ""


@pytest.mark.parametrize(
    "bad",
    [{"Env": "x"}, {"": "x"}, {"a b": "x"}, {"-a": "x"}, {"a": "X"}, {"a": "-x"}, {"a": "x" * 64}],
)
def test_bad_labels_are_rejected(bad: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        _pool(labels=bad)


def test_at_most_twenty_labels() -> None:
    assert validate_labels({f"k{i}": "v" for i in range(20)})
    with pytest.raises(ValueError, match="at most 20"):
        validate_labels({f"k{i}": "v" for i in range(21)})


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("env", ("env", "has", None)),
        ("!env", ("env", "not", None)),
        ("env=prod", ("env", "eq", "prod")),
        ("env!=prod", ("env", "ne", "prod")),
        ("env=", ("env", "eq", "")),
    ],
)
def test_selector_syntax(text: str, parsed: tuple[str, str, str | None]) -> None:
    assert parse_label_selector(text) == parsed


@pytest.mark.parametrize("text", ["", "Env=x", "env=X", "!", "=x"])
def test_bad_selectors_are_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        parse_label_selector(text)


def test_selectors_must_all_match() -> None:
    labels = {"env": "prod", "team": "pay"}
    assert labels_match(labels, [])
    assert labels_match(labels, ["env", "team=pay", "!legacy", "env!=dev"])
    assert not labels_match(labels, ["env=dev"])
    assert not labels_match(labels, ["env", "legacy"])
    assert not labels_match(labels, ["!env"])
    assert not labels_match(labels, ["team!=pay"])

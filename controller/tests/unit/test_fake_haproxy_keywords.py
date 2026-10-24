# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The fake engine must know every keyword the renderer emits.

``FakeHaproxy.validate`` treats an unknown first word as a syntax error, exactly as ``haproxy -c``
would. That is the right shape for the fake, but it means a directive added to the renderer and not to
``_KNOWN`` breaks every pipeline, worker and status test with "unknown keyword" — a failure that reads
as a broken engine rather than a stale test double.

This test compares the keywords actually present in every golden case against ``_KNOWN``, so the gap
shows up in a fast unit run instead of only in the container lane.
"""

from pathlib import Path

import pytest

from switchyard.adapters.fake_haproxy import _KNOWN

GOLDEN = Path(__file__).resolve().parents[1] / "golden"


def _cases() -> list[str]:
    return sorted(
        p.name for p in GOLDEN.iterdir() if (p / "expected" / "haproxy" / "haproxy.cfg").is_file()
    )


def _keywords(path: Path) -> set[str]:
    """First word of every non-comment, non-blank line of a rendered config."""
    words: set[str] = set()
    for line in path.read_text("utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        words.add(stripped.split()[0])
    return words


@pytest.mark.parametrize("case", _cases())
def test_every_rendered_keyword_is_known_to_the_fake(case: str) -> None:
    """A golden case that uses a keyword the fake rejects would fail the whole integration suite."""
    rendered = _keywords(GOLDEN / case / "expected" / "haproxy" / "haproxy.cfg")
    unknown = sorted(rendered - _KNOWN)
    assert not unknown, (
        f"{case} renders {unknown}, which FakeHaproxy treats as a syntax error; "
        "add them to _KNOWN in adapters/fake_haproxy.py"
    )

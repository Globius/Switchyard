"""Golden-file tests: sample state in, exact HAProxy files out."""

import pytest

from tests.golden_support import (
    case_names,
    expected_files,
    render_case,
    rendered_with_warnings,
    update_requested,
    write_expected,
)


def test_there_are_at_least_eight_cases() -> None:
    assert len(case_names()) >= 8


@pytest.mark.parametrize("name", case_names())
def test_rendered_files_match_expected(name: str) -> None:
    result = render_case(name)
    if update_requested():
        write_expected(name, result)
    assert rendered_with_warnings(result) == expected_files(name)


@pytest.mark.parametrize("name", case_names())
def test_rendering_twice_gives_identical_bytes(name: str) -> None:
    assert render_case(name).content_hash == render_case(name).content_hash

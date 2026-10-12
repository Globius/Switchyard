"""Every rendered file must be one the Data Plane API can store (engine push, ``storage_kind``)."""

import pytest

from switchyard.adapters.haproxy import storage_kind
from tests.golden_support import case_names, expected_files, render_case


@pytest.mark.parametrize("name", case_names())
def test_every_rendered_file_has_a_storage_kind(name: str) -> None:
    for path in render_case(name).files:
        storage_kind(path)


@pytest.mark.parametrize("name", case_names())
def test_every_expected_file_has_a_storage_kind(name: str) -> None:
    for path in expected_files(name):
        storage_kind(path)

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Every rendered file must be one the Data Plane API can store (engine push, ``storage_kind``).

A golden case renders the whole SE group: the HAProxy files the Data Plane API stores, and the
agent bundle files under ``coraza/`` that the agent writes itself (ADR 0144, 0228). The latter are
not Data Plane API objects, so they are listed here instead of classified.
"""

import pytest

from switchyard.adapters.haproxy import storage_kind
from tests.golden_support import case_names, expected_files, render_case

#: Directories the SE agent writes itself; a bundle file there is not a Data Plane API object.
AGENT_DIRS = ("coraza/",)


@pytest.mark.parametrize("name", case_names())
def test_every_rendered_file_has_a_storage_kind(name: str) -> None:
    for path in render_case(name).files:
        if path.startswith(AGENT_DIRS):
            continue
        storage_kind(path)


@pytest.mark.parametrize("name", case_names())
def test_every_expected_file_has_a_storage_kind(name: str) -> None:
    for path in expected_files(name):
        if path.startswith(AGENT_DIRS):
            continue
        storage_kind(path)

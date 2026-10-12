# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Every golden case must pass ``haproxy -c`` in the haproxy:3.2 image."""

import pytest

from tests.golden_support import case_names, render_case
from tests.haproxy_check import haproxy_check


@pytest.mark.parametrize("name", case_names())
def test_haproxy_accepts_rendered_config(name: str) -> None:
    result = haproxy_check(render_case(name).files)
    assert result.ok, result.output
    assert result.warnings == [], result.output

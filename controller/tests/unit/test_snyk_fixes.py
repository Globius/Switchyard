# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the Snyk Code fixes (ADR 0218)."""

from pathlib import Path

import pytest
from defusedxml.common import EntitiesForbidden
from defusedxml.ElementTree import fromstring

from switchyard.services.broker import BrokerError, write_broker_files


def test_xml_with_entities_is_refused() -> None:
    """defusedxml rejects entity declarations, the XXE vector."""
    bomb = '<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>'
    with pytest.raises(EntitiesForbidden):
        fromstring(bomb)


def test_broker_file_cannot_leave_output_dir(tmp_path: Path) -> None:
    """A relative name with ``..`` is refused."""
    with pytest.raises(BrokerError):
        write_broker_files({"../evil.yaml": "x"}, tmp_path / "out")

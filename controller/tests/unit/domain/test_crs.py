# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The CRS rule catalogue behind ``GET /waf/crs-rules`` (ADR 0152)."""

from switchyard.domain import crs


def test_every_rule_sits_in_the_family_its_id_belongs_to() -> None:
    ids = [r.id for g in crs.CATALOGUE for r in g.rules]
    assert len(ids) == len(set(ids))
    for group in crs.CATALOGUE:
        assert all(r.id // 1000 == group.prefix for r in group.rules), group.name
        assert all(1 <= r.paranoia_level <= 4 for r in group.rules)


def test_lookup_by_id() -> None:
    group = crs.group_of(942100)
    assert group is not None and group.name == "SQL injection"
    assert crs.find(920350) is not None
    assert crs.find(920999) is None and crs.group_of(100000) is None

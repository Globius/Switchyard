# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Device posture facts and the check against a policy (ADR 0196)."""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.models import PosturePolicySet
from switchyard.domain.posture import (
    OsMinimum,
    PostureFacts,
    PostureRequirements,
    evaluate,
    version_key,
)
from switchyard.domain.snapshot import validate_snapshot
from tests.factories import base_data

GOOD = PostureFacts(
    os="macos",
    os_version="14.5",
    disk_encrypted=True,
    firewall_enabled=True,
    screen_lock=True,
    days_since_update=3,
)


def test_versions_compare_numerically() -> None:
    assert version_key("10.0.19045") > version_key("10.0.9")
    assert version_key("14") == version_key("14.0.0.0")
    for bad in ("", "1.a", "1..2", "1.2.3.4.5", "-1", "v1"):
        with pytest.raises(ValueError, match="not a version"):
            version_key(bad)


def test_a_compliant_device_has_no_failures() -> None:
    assert evaluate(PostureRequirements(require_screen_lock=True), GOOD) == []


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"disk_encrypted": False}, ["disk_not_encrypted"]),
        ({"firewall_enabled": False}, ["firewall_off"]),
        ({"screen_lock": False}, ["screen_lock_off"]),
        ({"screen_lock": None}, ["screen_lock_unknown"]),
        ({"days_since_update": 46}, ["patch_too_old"]),
        ({"days_since_update": None}, ["patch_age_unknown"]),
        ({"os_version": "13.6"}, ["os_too_old"]),
        (
            {"disk_encrypted": False, "firewall_enabled": False},
            ["disk_not_encrypted", "firewall_off"],
        ),
    ],
)
def test_each_requirement_fails_on_its_own(update: dict[str, Any], expected: list[str]) -> None:
    requirements = PostureRequirements(
        minimums=[OsMinimum(os="macos", version="14.0")], require_screen_lock=True
    )
    assert evaluate(requirements, GOOD.model_copy(update=update)) == expected


def test_an_operating_system_outside_the_list_fails_alone() -> None:
    requirements = PostureRequirements(allowed_os=("windows",))
    assert evaluate(requirements, GOOD) == ["os_not_allowed"]


def test_optional_checks_can_be_switched_off() -> None:
    relaxed = PostureRequirements(
        require_disk_encryption=False, require_firewall=False, max_patch_age_days=None
    )
    facts = GOOD.model_copy(
        update={"disk_encrypted": False, "firewall_enabled": False, "days_since_update": None}
    )
    assert evaluate(relaxed, facts) == []


def test_requirement_and_fact_shapes_are_strict() -> None:
    with pytest.raises(ValidationError):
        PostureRequirements(allowed_os=())
    with pytest.raises(ValidationError, match="unique"):
        PostureRequirements(allowed_os=("linux", "linux"))
    with pytest.raises(ValidationError, match="minimums"):
        PostureRequirements(allowed_os=("linux",), minimums=[OsMinimum(os="windows", version="10")])
    with pytest.raises(ValidationError):
        PostureRequirements(max_patch_age_days=0)
    for bad in (
        {"os": "beos"},
        {"os_version": "14.x"},
        {"days_since_update": -1},
        {"hostname": "bad host"},
        {"agent_version": "x" * 33},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            PostureFacts.model_validate({**GOOD.model_dump(), **bad})


def policy(**kw: Any) -> dict[str, Any]:
    return {
        "id": "ps-posture",
        "tenant_id": "t-acme",
        "name": "managed",
        "kind": "posture",
        "pki_role": "acme-devices",
        **kw,
    }


def test_the_policy_set_defaults_and_limits() -> None:
    p = PosturePolicySet(id=uuid.uuid4(), tenant_id=uuid.uuid4(), name="m", pki_role="acme-d")
    assert p.cert_ttl_h == 12 and p.requirements.require_disk_encryption
    for bad in ({"cert_ttl_h": 0}, {"cert_ttl_h": 73}, {"pki_role": "has space"}):
        with pytest.raises(ValidationError):
            PosturePolicySet.model_validate(
                {"id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "name": "m", "pki_role": "r", **bad}
            )


def codes(*policies: dict[str, Any]) -> list[str]:
    data = base_data()
    data["policy_sets"] = list(policies)
    return [p.code for p in validate_snapshot(snapshot_from_mapping(data))]


def test_the_pki_role_stays_in_the_tenants_namespace() -> None:
    assert codes(policy()) == []
    assert codes(policy(pki_role="globex-devices")) == ["pki_role_not_allowed"]


def test_a_posture_set_cannot_be_attached_to_a_service() -> None:
    data = base_data()
    data["policy_sets"] = [policy()]
    data["virtual_services"][0]["policy_set_ids"] = ["ps-posture"]
    found = [p.code for p in validate_snapshot(snapshot_from_mapping(data))]
    assert "wrong_policy_kind" in found

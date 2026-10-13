# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""What the services store must parse into the API read models (found on the lab: a stored key
the model did not know made two list endpoints answer 500)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from switchyard.api.schemas import AgentStatusRead, KubernetesSyncRead
from switchyard.domain.models import RadiusServerSpec
from switchyard.services.kubernetes import _with_last_change


def _agent(**extra: object) -> dict[str, object]:
    return {
        "version": "1.0",
        "bundle_id": "b1",
        "haproxy_healthy": True,
        "routes_withdrawn": False,
        "bgp": [],
        "vrrp": [],
        "file_hashes": {},
        **extra,
    }


def test_the_load_the_status_poller_stores_reads_back() -> None:
    read = AgentStatusRead.model_validate(_agent(load={"conns": 3, "max_conn": 20000}))
    assert read.load is not None and (read.load.conns, read.load.max_conn) == (3, 20000)
    assert AgentStatusRead.model_validate(_agent()).load is None


def test_the_last_change_a_kubernetes_sync_stores_reads_back() -> None:
    summary = {
        "planned_services": 1,
        "created": 1,
        "updated": 0,
        "deleted": 0,
        "change_set_id": str(uuid4()),
    }
    stored = _with_last_change(summary, None, datetime(2026, 10, 13, tzinfo=UTC))
    read = KubernetesSyncRead.model_validate(stored)
    assert read.last_change is not None and read.last_change.created == 1
    # a later sync with nothing to do keeps it
    again = KubernetesSyncRead.model_validate(
        _with_last_change(
            {"planned_services": 1, "created": 0, "updated": 0, "deleted": 0}, stored, None
        )
    )
    assert again.last_change == read.last_change


def test_a_radius_server_may_be_an_address_but_not_a_local_one() -> None:
    ok = {"name": "main", "port": 1812, "secret_ref": "kv/tenants/t/auth/radius"}
    assert RadiusServerSpec(host="172.30.0.250", **ok).host == "172.30.0.250"
    assert RadiusServerSpec(host="Radius.Example.Test", **ok).host == "radius.example.test"
    for bad in ("127.0.0.1", "169.254.169.254", "::1", "0.0.0.0"):  # noqa: S104
        with pytest.raises(ValidationError):
            RadiusServerSpec(host=bad, **ok)

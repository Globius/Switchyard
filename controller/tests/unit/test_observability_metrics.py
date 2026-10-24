# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-tenant labels on the controller's ``/metrics`` and their cardinality bounds.

The bounds are asserted on the pure :func:`switchyard.services.observability.metric_families`, so
they hold whatever the traffic: a label value comes from a tenant row, a domain enum or a virtual
service row, and a per-request value (request id, host, SNI, upstream) never becomes a label.
"""

import uuid
from datetime import UTC, datetime, timedelta

from switchyard.services.observability import (
    SYSTEM_TENANT,
    EngineSample,
    MetricRows,
    ServiceSample,
    metric_families,
    render_exposition,
)

ACME = uuid.UUID(int=1)
GLOBEX = uuid.UUID(int=2)
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def rows() -> MetricRows:
    return MetricRows(
        jobs=(
            (ACME, "apply", "queued", 2),
            (GLOBEX, "apply", "queued", 1),
            (None, "gc", "done", 5),
        ),
        job_seconds=((ACME, "apply", "done", 4, 12.5), (GLOBEX, "apply", "failed", 1, 3.0)),
        change_sets=((ACME, "draft", 2), (GLOBEX, "failed", 1)),
        engines=(
            EngineSample(ACME, True, False, 0, NOW - timedelta(seconds=30)),
            EngineSample(ACME, False, True, 2, NOW - timedelta(seconds=600)),
            EngineSample(GLOBEX, True, False, 0, NOW - timedelta(seconds=10)),
        ),
        services=(
            ServiceSample(ACME, "shop", "http", True),
            ServiceSample(ACME, "dns", "udp", False),
            ServiceSample(GLOBEX, "web", "http", True),
        ),
    )


def families() -> dict[str, list[tuple[str, dict[str, str], float]]]:
    return {
        f.name: [(suffix, dict(labels), value) for suffix, labels, value in f.samples]
        for f in metric_families(rows(), now=NOW, version="0.1.0")
    }


def test_every_tenant_scoped_series_names_a_tenant_the_controller_holds() -> None:
    tenants = {str(ACME), str(GLOBEX), SYSTEM_TENANT}
    for name, samples in families().items():
        if name == "switchyard_info":
            continue
        for _, labels, _ in samples:
            assert labels["tenant"] in tenants, (name, labels)


def test_a_job_without_a_tenant_is_labelled_system_not_dropped() -> None:
    job = next(s for s in families()["switchyard_jobs"] if s[1]["kind"] == "gc")
    assert (job[1]["tenant"], job[2]) == (SYSTEM_TENANT, 5)


def test_the_per_service_label_is_bounded_by_the_virtual_service_rows() -> None:
    services = families()["switchyard_virtual_services"]
    assert {labels["service"] for _, labels, _ in services} == {"shop", "dns", "web"}
    # one series per row: deleting a virtual service drops its series
    assert len(services) == 3
    assert all(labels["tenant"] in {str(ACME), str(GLOBEX)} for _, labels, _ in services)


def test_series_counts_are_the_size_of_the_configuration_not_of_the_traffic() -> None:
    fam = families()
    tenants = len({str(ACME), str(GLOBEX), SYSTEM_TENANT})
    # jobs: one series per tenant, kind and state in the rows, so at most tenants x kinds x states
    assert len(fam["switchyard_jobs"]) <= tenants * 14 * 6
    # durations are a summary: at most two samples per tenant, kind and outcome
    assert len(fam["switchyard_job_duration_seconds"]) <= 2 * tenants * 14 * 6
    # engines: two reachability series per tenant that has an engine
    assert len(fam["switchyard_engines"]) == 2 * 2


def test_no_per_request_value_is_a_label() -> None:
    for samples in families().values():
        for _, labels, _ in samples:
            assert not {"request_id", "host", "sni", "upstream", "client", "path"} & set(labels)


def test_engines_are_counted_per_tenant_with_both_reachability_values() -> None:
    samples = families()["switchyard_engines"]
    by_tenant = {(labels["tenant"], labels["reachable"]): value for _, labels, value in samples}
    assert by_tenant[(str(ACME), "true")] == 1
    assert by_tenant[(str(ACME), "false")] == 1
    assert by_tenant[(str(GLOBEX), "false")] == 0


def test_status_age_is_the_newest_poll_of_each_tenant() -> None:
    ages = {
        labels["tenant"]: value for _, labels, value in families()["switchyard_status_age_seconds"]
    }
    assert ages == {str(ACME): 30.0, str(GLOBEX): 10.0}


def test_the_exposition_carries_the_tenant_label() -> None:
    body = render_exposition(metric_families(rows(), now=NOW, version="0.1.0"))
    assert f'switchyard_engines{{reachable="true",tenant="{ACME}"}} 1' in body
    assert f'switchyard_change_sets{{state="failed",tenant="{GLOBEX}"}} 1' in body
    assert f'switchyard_jobs{{kind="gc",state="done",tenant="{SYSTEM_TENANT}"}} 5' in body

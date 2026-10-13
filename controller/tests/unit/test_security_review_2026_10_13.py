# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fixes of the 2026-10-13 security review (docs/security/2026-10-13-review.md)."""

import pytest

from switchyard.adapters.dns.base import check_endpoint
from switchyard.api.grafana_proxy import _clean_path, _denied
from switchyard.api.problems import ApiError
from switchyard.domain.snapshot import pki_role_owner


@pytest.mark.parametrize(
    "path",
    [
        "api/users?",  # %3F: decoded before routing, hides the segment from the deny list
        "api/orgs?x=1",
        "api/admin#frag",
        "api/users;x",
        "api//admin/users",  # empty segment
        "d/../api/admin/users",
        "./api/users",
        "api/users\n",
        "api\admin",
    ],
)
def test_grafana_paths_the_deny_list_could_misread_are_refused(path: str) -> None:
    with pytest.raises(ApiError):
        _clean_path(path)


@pytest.mark.parametrize(
    "path", ["d/abc/slug", "explore", "api/ds/query", "", "d/abc/", "public/x.js"]
)
def test_ordinary_grafana_paths_pass(path: str) -> None:
    assert _clean_path(path) == path


@pytest.mark.parametrize("path", ["api/admin/users", "API/Users", "/api/orgs/", "Logout"])
def test_the_grafana_deny_list_ignores_case(path: str) -> None:
    assert _denied(path)


@pytest.mark.parametrize(
    "host",
    ["100.100.100.200", "100.64.0.1", "169.254.169.254", "::ffff:169.254.169.254", "127.0.0.1"],
)
def test_endpoints_in_shared_or_link_local_space_are_refused(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SWITCHYARD_DNS_CIDRS", raising=False)
    monkeypatch.delenv("SWITCHYARD_ENGINE_POLICY", raising=False)
    with pytest.raises(ValueError, match="may contact"):
        check_endpoint(host, "server")


def test_shared_space_is_allowed_when_the_operator_lists_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWITCHYARD_DNS_CIDRS", "100.64.0.0/10")
    check_endpoint("100.64.0.1", "server")


def test_the_longest_tenant_name_owns_a_pki_role() -> None:
    names = ["acme", "acme-corp", "other"]
    assert pki_role_owner("acme-devices", names) == "acme"
    assert pki_role_owner("acme-corp-devices", names) == "acme-corp"
    assert pki_role_owner("shared-devices", names) is None


@pytest.mark.parametrize("value", ["change-me", "CHANGE-ME", "short", "password"])
def test_a_placeholder_or_short_metrics_token_stops_startup(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from switchyard import runtime

    monkeypatch.setenv("SWITCHYARD_METRICS_TOKEN", value)
    with pytest.raises(RuntimeError, match="placeholder"):
        runtime.metrics_token()


def test_a_real_or_absent_metrics_token_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    from switchyard import runtime

    monkeypatch.delenv("SWITCHYARD_METRICS_TOKEN", raising=False)
    assert runtime.metrics_token() is None
    monkeypatch.setenv("SWITCHYARD_METRICS_TOKEN", "a" * 32)
    assert runtime.metrics_token() == "a" * 32

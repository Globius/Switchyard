# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Controller cluster members: validation, Patroni's answer, the client's target check."""

import pytest

from switchyard.adapters.patroni import FakePatroni, HttpPatroni, PatroniError, check_target
from switchyard.domain.controller_members import (
    bad_address,
    check_member,
    is_external,
    parse_cluster,
)

CLUSTER = {
    "scope": "switchyard",
    "members": [
        {"name": "ctl-1", "role": "leader", "state": "running", "timeline": 4, "host": "10.0.0.11"},
        {"name": "ctl-2", "role": "sync_standby", "state": "streaming", "timeline": 4, "lag": 0},
        {
            "name": "ctl-esxi",
            "role": "replica",
            "state": "streaming",
            "timeline": 4,
            "lag": 2048,
            "tags": {"nofailover": True},
        },
        {"name": "ctl-x", "role": "replica", "state": "start failed", "lag": "unknown"},
        {"role": "replica"},
        "junk",
    ],
}


def test_parse_cluster_reads_roles_lag_and_the_nofailover_tag() -> None:
    states = parse_cluster(CLUSTER)
    assert set(states) == {"ctl-1", "ctl-2", "ctl-esxi", "ctl-x"}
    assert states["ctl-1"].role == "leader" and states["ctl-1"].lag_bytes is None
    assert states["ctl-2"].lag_bytes == 0 and not states["ctl-2"].nofailover
    assert states["ctl-esxi"].lag_bytes == 2048 and states["ctl-esxi"].nofailover
    assert states["ctl-x"].lag_bytes is None  # "unknown" is not a number
    assert states["ctl-x"].state == "start failed"
    assert parse_cluster({}) == {} and parse_cluster({"members": None}) == {}


def test_bad_lag_and_flags_are_not_trusted() -> None:
    odd = parse_cluster({"members": [{"name": "a", "lag": True, "timeline": "4", "tags": []}]})
    assert odd["a"].lag_bytes is None and odd["a"].timeline is None and not odd["a"].nofailover


@pytest.mark.parametrize(
    ("host", "bad"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("169.254.169.254", True),
        ("0.0.0.0", True),  # noqa: S104
        ("224.0.0.1", True),
        ("10.0.0.13", False),
        ("ctl3.example.test", False),
        ("[2001:db8::13]", False),
    ],
)
def test_addresses_the_controller_must_not_contact(host: str, bad: bool) -> None:
    assert bad_address(host) is bad


def test_a_good_member_has_no_problems() -> None:
    assert check_member("ctl-esxi", "10.0.0.13", "esxi", "http://10.0.0.13:8008", "esxi-01") == []
    assert check_member("ctl-3", "ctl3.example.test", "proxmox", None, None) == []
    assert (
        check_member("ctl-3", "10.0.0.13", "other", "https://ctl3.example.test:8008/", None) == []
    )


@pytest.mark.parametrize(
    ("args", "field"),
    [
        (("Bad Name", "10.0.0.13", "esxi", None, None), "name"),
        (("a", "127.0.0.1", "esxi", None, None), "address"),
        (("a", "not a host!", "esxi", None, None), "address"),
        (("a", "10.0.0.13", "vmware", None, None), "platform"),
        (("a", "10.0.0.13", "esxi", "ftp://10.0.0.13", None), "patroni_url"),
        (("a", "10.0.0.13", "esxi", "http://10.0.0.13:8008/cluster", None), "patroni_url"),
        (("a", "10.0.0.13", "esxi", "http://169.254.169.254", None), "patroni_url"),
        (("a", "10.0.0.13", "esxi", "http://u:p@10.0.0.13", None), "patroni_url"),
        (("a", "10.0.0.13", "esxi", None, "<script>"), "host"),
    ],
)
def test_bad_members_name_the_field(
    args: tuple[str, str, str, str | None, str | None], field: str
) -> None:
    assert field in {f for f, _ in check_member(*args)}


def test_external_means_not_on_proxmox_by_default() -> None:
    assert is_external("esxi") and is_external("other") and not is_external("proxmox")


async def test_fake_patroni_answers_or_refuses() -> None:
    fake = FakePatroni({"http://a:8008": CLUSTER})
    assert (await fake.cluster("http://a:8008"))["scope"] == "switchyard"
    fake.down.add("http://a:8008")
    with pytest.raises(PatroniError):
        await fake.cluster("http://a:8008")
    with pytest.raises(PatroniError):
        await fake.cluster("http://unknown:8008")
    assert fake.asked == ["http://a:8008", "http://a:8008", "http://unknown:8008"]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8008", "http://169.254.169.254", "http://[::1]"])
def test_the_client_refuses_loopback_and_metadata(url: str) -> None:
    with pytest.raises(PatroniError, match="may contact"):
        check_target(url)


def test_the_client_accepts_a_private_address() -> None:
    check_target("http://10.0.0.13:8008")


async def test_the_client_refuses_before_connecting() -> None:
    with pytest.raises(PatroniError, match="may contact"):
        await HttpPatroni().cluster("http://127.0.0.1:1")


def test_the_client_trusts_the_configured_ca(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWITCHYARD_PATRONI_CA", "/run/switchyard/mtls/ca.crt")
    assert HttpPatroni()._verify == "/run/switchyard/mtls/ca.crt"
    assert HttpPatroni(ca_file="/x.pem")._verify == "/x.pem"
    monkeypatch.delenv("SWITCHYARD_PATRONI_CA")
    assert HttpPatroni()._verify is True

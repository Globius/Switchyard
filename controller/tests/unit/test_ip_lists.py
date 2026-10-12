"""Country and reputation lists (ADR 0147): parsing, the HTTP source, the model and the rules."""

import copy
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from switchyard.adapters.iplists import HttpIpLists, IpListError, parse_networks
from switchyard.domain import models as dm
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import validate_snapshot
from switchyard.services.iplists import merge_networks, shrinks_too_far
from tests.factories import BASE

# -- parsing --------------------------------------------------------------------------------------


def test_comments_blank_lines_and_trailing_notes_are_ignored() -> None:
    text = "; Spamhaus DROP\n\n192.0.2.0/24 ; SBL1\n# other\n198.51.100.0/24\n2001:db8::/32 # v6\n"
    assert parse_networks(text) == ["192.0.2.0/24", "198.51.100.0/24", "2001:db8::/32"]


def test_single_addresses_become_host_networks_and_duplicates_collapse() -> None:
    assert parse_networks("198.51.100.7\n198.51.100.7/32\n2001:db8::1\n") == [
        "198.51.100.7/32",
        "2001:db8::1/128",
    ]


def test_host_bits_are_cleared_instead_of_refused() -> None:
    assert parse_networks("192.0.2.5/24\n") == ["192.0.2.0/24"]


def test_a_web_page_is_not_a_list() -> None:
    with pytest.raises(IpListError, match="not a list of networks"):
        parse_networks("<html>\n<body>\nForbidden\n</body>\n</html>\n")


def test_a_few_bad_lines_in_a_big_list_are_tolerated() -> None:
    text = "\n".join(f"10.{i}.0.0/16" for i in range(100)) + "\nnot-a-network\n"
    assert len(parse_networks(text)) == 100


def test_an_empty_answer_parses_to_nothing() -> None:
    assert parse_networks("# nothing today\n") == []


def test_merge_keeps_ipv4_first_in_address_order() -> None:
    assert merge_networks([["10.0.0.0/8", "2001:db8::/32"], ["9.0.0.0/8", "10.0.0.0/8"]]) == [
        "9.0.0.0/8",
        "10.0.0.0/8",
        "2001:db8::/32",
    ]


def test_the_shrink_guard_only_applies_to_lists_big_enough_to_matter() -> None:
    assert shrinks_too_far(1000, 100)
    assert not shrinks_too_far(1000, 500)
    assert not shrinks_too_far(10, 1)
    assert not shrinks_too_far(0, 0)


# -- the HTTP source ------------------------------------------------------------------------------


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/xx.zone":
        return httpx.Response(200, text="192.0.2.0/24\n198.51.100.0/24\n")
    if request.url.path == "/drop":
        return httpx.Response(200, text="; header\n203.0.113.0/24 ; SBL\n")
    if request.url.path == "/page":
        return httpx.Response(200, text="<html>\n<body>\nx\n</body>\n</html>\n")
    if request.url.path == "/moved":
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
    return httpx.Response(404)


def source(**kw: Any) -> HttpIpLists:
    return HttpIpLists(
        kw.get("geo", "https://lists.invalid/{cc}.zone"),
        kw.get(
            "feeds",
            {
                "drop": "https://lists.invalid/drop",
                "page": "https://lists.invalid/page",
                "moved": "https://lists.invalid/moved",
            },
        ),
        transport=httpx.MockTransport(handler),
    )


async def test_a_country_is_fetched_by_lower_case_code() -> None:
    assert await source().country("XX") == ["192.0.2.0/24", "198.51.100.0/24"]


async def test_a_feed_is_fetched_by_name() -> None:
    assert await source().feed("drop") == ["203.0.113.0/24"]


async def test_failures_are_list_errors() -> None:
    s = source()
    with pytest.raises(IpListError, match="HTTP 404"):
        await s.country("ZZ")
    with pytest.raises(IpListError, match="not a list"):
        await s.feed("page")
    with pytest.raises(IpListError, match="not configured"):
        await s.feed("unknown")


async def test_redirects_are_not_followed() -> None:
    # a feed host must not be able to point the controller at an internal address
    with pytest.raises(IpListError, match="HTTP 302"):
        await source().feed("moved")


async def test_without_a_template_countries_are_off() -> None:
    s = HttpIpLists(None, {"drop": "https://lists.invalid/drop"})
    assert not s.has_geo() and s.feeds() == frozenset({"drop"})
    with pytest.raises(IpListError, match="not configured"):
        await s.country("XX")


@pytest.mark.parametrize(
    ("geo", "feeds"),
    [
        ("https://x.invalid/list.zone", {}),  # no placeholder
        (None, {"a": "ftp://x.invalid/a"}),
        (None, {"a": "file:///etc/passwd"}),
    ],
)
def test_bad_source_configuration_is_refused(geo: str | None, feeds: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        HttpIpLists(geo, feeds)


# -- model and rules ------------------------------------------------------------------------------


def policy(**kw: Any) -> dm.AllowListPolicySet:
    return dm.AllowListPolicySet(id=UUID(int=1), tenant_id=UUID(int=2), name="l", **kw)


def test_a_geo_list_may_start_empty() -> None:
    assert policy(managed_by="geo", countries=("NL", "DE")).cidrs == ()


def test_a_reputation_list_may_start_empty() -> None:
    assert policy(managed_by="reputation", feed="spamhaus-drop").cidrs == ()


@pytest.mark.parametrize(
    "kw",
    [
        {},  # an unmanaged list needs networks
        {"managed_by": "cloudflare"},
        {"managed_by": "geo"},  # needs countries
        {"managed_by": "geo", "countries": ("nl",)},  # upper case
        {"managed_by": "geo", "countries": ("NLD",)},
        {"managed_by": "geo", "countries": ("NL", "NL")},
        {"managed_by": "geo", "countries": ("NL",), "feed": "x"},
        {"managed_by": "reputation"},  # needs a feed
        {"managed_by": "reputation", "feed": "x", "countries": ("NL",)},
        {"managed_by": "reputation", "feed": "Not A Name"},
        {"cidrs": ("192.0.2.0/24",), "countries": ("NL",)},
    ],
)
def test_inconsistent_lists_are_refused(kw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        policy(**kw)


def data(**service: Any) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    d["policy_sets"] = [
        {
            "id": "ps-geo",
            "tenant_id": "t-acme",
            "name": "geo",
            "kind": "allow_list",
            "managed_by": "geo",
            "countries": ["XX"],
        },
        {
            "id": "ps-redirect",
            "tenant_id": "t-acme",
            "name": "redirects",
            "kind": "redirect",
            "rules": [
                {"match": {"path_prefix": "/old"}, "redirect": {"location": "/new", "code": 301}}
            ],
        },
    ]
    d["virtual_services"][0].update(service)
    return d


def codes(d: dict[str, Any]) -> list[str]:
    return [p.code for p in validate_snapshot(snapshot_from_mapping(d))]


def test_a_service_may_deny_a_list() -> None:
    assert codes(data(deny_list_id="ps-geo")) == []


def test_the_deny_list_must_exist_and_be_an_ip_list() -> None:
    assert "unknown_reference" in codes(data(deny_list_id="nope"))
    assert "wrong_policy_kind" in codes(data(deny_list_id="ps-redirect"))


def test_udp_services_cannot_use_a_deny_list() -> None:
    d = data(deny_list_id="ps-geo")
    d["virtual_services"][0].update(
        protocol="udp",
        ports=[{"port": 5353}],
        routes=[],
        certificate_ids=[],
        allow_list_id=None,
        default_pool_id="pool-web",
    )
    d["virtual_services"][0]["ports"] = [{"port": 5353}]
    assert "udp_field_not_allowed" in codes(d)

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The CrowdSec local API client: which decisions count, the key header, and failures (ADR 0213)."""

import json

import httpx
import pytest

from switchyard.adapters.crowdsec import (
    CrowdSecError,
    FakeCrowdSec,
    HttpCrowdSec,
    check_url,
    parse_decisions,
)
from switchyard.db.crowdsec_config import normalize_origins

STREAM = {
    "new": [
        {"scope": "Ip", "value": "192.0.2.9", "type": "ban", "origin": "crowdsec"},
        {"scope": "Range", "value": "198.51.100.0/24", "type": "ban", "origin": "CAPI"},
        {"scope": "Ip", "value": "203.0.113.5", "type": "captcha", "origin": "crowdsec"},
        {"scope": "Country", "value": "XX", "type": "ban", "origin": "cscli"},
        {"scope": "Ip", "value": "2001:db8::7", "type": "ban", "origin": "crowdsec"},
    ],
    "deleted": None,
}


def test_only_ban_decisions_on_addresses_are_read() -> None:
    assert parse_decisions(json.dumps(STREAM)) == [
        "192.0.2.9/32",
        "198.51.100.0/24",
        "2001:db8::7/128",
    ]


def test_decisions_can_be_limited_by_origin() -> None:
    assert parse_decisions(json.dumps(STREAM), frozenset({"crowdsec"})) == [
        "192.0.2.9/32",
        "2001:db8::7/128",
    ]


def test_origins_can_be_excluded() -> None:
    assert parse_decisions(json.dumps(STREAM), exclude_origins=frozenset({"CAPI"})) == [
        "192.0.2.9/32",
        "2001:db8::7/128",
    ]


@pytest.mark.parametrize("body", ["not json", "[]", '{"new": 3}', '{"new": ["x"]}'])
def test_a_wrong_answer_is_not_a_decision_stream(body: str) -> None:
    with pytest.raises(CrowdSecError):
        parse_decisions(body)


def test_no_new_decisions_is_an_empty_list() -> None:
    assert parse_decisions('{"new": null, "deleted": null}') == []


def test_origins_are_split_and_trimmed() -> None:
    assert normalize_origins(" crowdsec, CAPI ,,") == ["crowdsec", "CAPI"]
    assert normalize_origins(["a", " "]) == ["a"]


@pytest.mark.parametrize(
    "url", ["ftp://x.invalid", "https://u:p@x.invalid", "https://x.invalid/?a=b", "x.invalid", ""]
)
def test_urls_must_be_plain_http(url: str) -> None:
    with pytest.raises(ValueError, match="http"):
        check_url(url)


def client(seen: list[httpx.Request], status: int = 200) -> HttpCrowdSec:
    def lapi(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("x-api-key") != "bouncer-key":
            return httpx.Response(403)
        return httpx.Response(status, json=STREAM)

    return HttpCrowdSec(transport=httpx.MockTransport(lapi))


async def test_the_stream_is_read_with_the_bouncer_key() -> None:
    seen: list[httpx.Request] = []
    nets = await client(seen).decisions("https://lapi.invalid:8080/", "bouncer-key")
    assert nets == ["192.0.2.9/32", "198.51.100.0/24", "2001:db8::7/128"]
    assert str(seen[0].url) == "https://lapi.invalid:8080/v1/decisions/stream?startup=true"


async def test_a_refused_key_or_a_bad_status_is_an_error() -> None:
    with pytest.raises(CrowdSecError, match="HTTP 403"):
        await client([]).decisions("https://lapi.invalid:8080", "wrong")
    with pytest.raises(CrowdSecError, match="HTTP 500"):
        await client([], 500).decisions("https://lapi.invalid:8080", "bouncer-key")


async def test_an_unusable_url_is_an_error_not_a_crash() -> None:
    with pytest.raises(CrowdSecError, match="not usable"):
        await client([]).decisions("ftp://lapi.invalid", "bouncer-key")


async def test_the_fake_checks_the_key() -> None:
    fake = FakeCrowdSec(banned=["192.0.2.0/24"])
    assert await fake.decisions("https://x.invalid", "bouncer-key") == ["192.0.2.0/24"]
    with pytest.raises(CrowdSecError):
        await fake.decisions("https://x.invalid", "nope")

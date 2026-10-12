# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The Cloudflare REST client against a mocked API."""

import json

import httpx
import pytest

from switchyard.adapters.cloudflare import CloudflareError, HttpCloudflare


class Api:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []
        self.zone_lookups = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/client/v4")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        assert request.headers["authorization"] == "Bearer tok"
        if path == "/zones":
            self.zone_lookups += 1
            name = request.url.params["name"]
            zones = [{"id": "z1", "name": name}] if name == "example.test" else []
            return httpx.Response(200, json={"success": True, "result": zones})
        if path == "/zones/z1/dns_records" and request.method == "POST":
            return httpx.Response(200, json={"success": True, "result": {"id": "r9"}})
        if path.startswith("/zones/z1/dns_records/") and request.method == "DELETE":
            return httpx.Response(200, json={"success": True, "result": {"id": "r9"}})
        if path == "/certificates":
            return httpx.Response(200, json={"success": True, "result": {"certificate": "PEM"}})
        if path == "/ips":
            result = {"ipv4_cidrs": ["192.0.2.0/24"], "ipv6_cidrs": ["2001:db8::/32"]}
            return httpx.Response(200, json={"success": True, "result": result})
        return httpx.Response(
            403, json={"success": False, "errors": [{"message": "no permission"}]}
        )


def client(api: Api) -> HttpCloudflare:
    return HttpCloudflare(
        "tok",
        base_url="https://api.cloudflare.com/client/v4",
        transport=httpx.MockTransport(api.handler),
    )


async def test_txt_record_goes_into_the_matching_zone_and_can_be_deleted() -> None:
    api = Api()
    cf = client(api)
    rid = await cf.create_txt("_acme-challenge.www.example.test", "token", ttl=10)
    assert rid == "z1/r9"
    post = next(b for m, p, b in api.requests if m == "POST")
    assert post == {
        "type": "TXT",
        "name": "_acme-challenge.www.example.test",
        "content": "token",
        "ttl": 60,
    }
    await cf.create_txt("_acme-challenge.example.test", "t2")
    assert api.zone_lookups == 2, "the zone id is cached after the first hit"
    await cf.delete_txt("_acme-challenge.www.example.test", rid)
    assert ("DELETE", "/zones/z1/dns_records/r9", None) in api.requests


async def test_origin_ca_and_ip_ranges() -> None:
    cf = client(Api())
    assert await cf.issue_origin_ca(("example.test",), "CSR") == "PEM"
    ranges = await cf.ip_ranges()
    assert ranges.all == ("192.0.2.0/24", "2001:db8::/32")


async def test_errors_carry_cloudflares_message_and_unknown_zones_fail() -> None:
    cf = client(Api())
    with pytest.raises(CloudflareError, match="no Cloudflare zone"):
        await cf.create_txt("_acme-challenge.other.invalid", "x")
    broken = HttpCloudflare(
        "tok",
        base_url="https://api.cloudflare.com/client/v4",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                403, json={"success": False, "errors": [{"message": "no permission"}]}
            )
        ),
    )
    with pytest.raises(CloudflareError, match="no permission"):
        await broken.ip_ranges()

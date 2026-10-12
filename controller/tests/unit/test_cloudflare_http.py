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


class AddressApi:
    """Zone z1 with a record store: GET filters by type and name, PUT/POST write."""

    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = {str(r["id"]): r for r in records}
        self.requests: list[tuple[str, str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/client/v4")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))

        def ok(result: object) -> httpx.Response:
            return httpx.Response(200, json={"success": True, "result": result})

        if path == "/zones":
            return ok([{"id": "z1"}] if request.url.params["name"] == "example.test" else [])
        if path == "/zones/z1/dns_records" and request.method == "GET":
            want = request.url.params
            return ok(
                [
                    r
                    for r in self.records.values()
                    if r["type"] == want["type"] and r["name"] == want["name"]
                ]
            )
        if path == "/zones/z1/dns_records" and request.method == "POST":
            self.records["new"] = {"id": "new", **(body or {})}
            return ok({"id": "new"})
        rid = path.rsplit("/", 1)[-1]
        if request.method == "PUT" and rid in self.records:
            self.records[rid] = {"id": rid, **(body or {})}
            return ok({"id": rid})
        return httpx.Response(404, json={"success": False, "errors": [{"message": "gone"}]})


def address_client(api: AddressApi) -> HttpCloudflare:
    return HttpCloudflare(
        "tok",
        base_url="https://api.cloudflare.com/client/v4",
        transport=httpx.MockTransport(api.handler),
    )


async def test_address_record_is_created_with_proxy_and_automatic_ttl() -> None:
    api = AddressApi([])
    handle = await address_client(api).upsert_address(
        "app.example.test", "192.0.2.20", ttl=300, proxied=True, handle=None
    )
    assert handle == "z1/new"
    assert api.records["new"] | {"id": 0} == {
        "id": 0,
        "type": "A",
        "name": "app.example.test",
        "content": "192.0.2.20",
        "ttl": 1,
        "proxied": True,
    }


async def test_an_ipv6_address_makes_an_aaaa_record() -> None:
    api = AddressApi([])
    await address_client(api).upsert_address(
        "app.example.test", "2001:db8::1", ttl=300, proxied=False, handle=None
    )
    assert api.records["new"]["type"] == "AAAA"
    assert api.records["new"]["ttl"] == 300


async def test_a_known_handle_is_updated_in_place() -> None:
    api = AddressApi(
        [{"id": "r1", "type": "A", "name": "app.example.test", "content": "192.0.2.1"}]
    )
    handle = await address_client(api).upsert_address(
        "app.example.test", "192.0.2.2", ttl=300, proxied=False, handle="z1/r1"
    )
    assert handle == "z1/r1"
    assert api.records["r1"]["content"] == "192.0.2.2"


async def test_a_handle_deleted_by_hand_falls_back_to_creating() -> None:
    api = AddressApi([])
    handle = await address_client(api).upsert_address(
        "app.example.test", "192.0.2.2", ttl=300, proxied=False, handle="z1/missing"
    )
    assert handle == "z1/new"


async def test_an_identical_existing_record_is_adopted_and_a_different_one_refused() -> None:
    same = AddressApi([{"id": "r1", "type": "A", "name": "a.example.test", "content": "192.0.2.2"}])
    handle = await address_client(same).upsert_address(
        "a.example.test", "192.0.2.2", ttl=300, proxied=False, handle=None
    )
    assert handle == "z1/r1"
    other = AddressApi(
        [{"id": "r1", "type": "A", "name": "a.example.test", "content": "198.51.100.9"}]
    )
    with pytest.raises(CloudflareError, match="did not create"):
        await address_client(other).upsert_address(
            "a.example.test", "192.0.2.2", ttl=300, proxied=False, handle=None
        )
    assert other.records["r1"]["content"] == "198.51.100.9"

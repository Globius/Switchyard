"""The PowerDNS HTTP client (ADR 0160)."""

import json
from typing import Any

import httpx
import pytest

from switchyard.adapters.powerdns import FakePowerDns, HttpPowerDns, PowerDnsError, fqdn
from switchyard.domain.gslb import Rrset


class Server:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.zones: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "k"
        body: Any = json.loads(request.content) if request.content else None
        path = request.url.raw_path.decode()
        self.calls.append((request.method, path, body))
        base = "/api/v1/servers/localhost"
        if path == f"{base}/zones" and request.method == "POST":
            self.zones.add(body["name"])
            return httpx.Response(201, json={})
        if path.startswith(f"{base}/zones/") and path.endswith("/cryptokeys"):
            return httpx.Response(
                200,
                json=[
                    {"keytype": "zsk", "active": True, "ds": []},
                    {"keytype": "ksk", "active": True, "ds": ["1 13 2 aa"]},
                    {"keytype": "ksk", "active": False, "ds": ["z. IN DS 2 13 2 bb"]},
                ],
            )
        if path.startswith(f"{base}/zones/"):
            zone = path.rsplit("/", 1)[1].replace("%2E", ".")
            if request.method == "GET":
                if zone not in self.zones:
                    return httpx.Response(404, json={"error": "Could not find domain"})
                return httpx.Response(
                    200,
                    json={
                        "rrsets": [
                            {
                                "name": "w.z.",
                                "type": "LUA",
                                "ttl": 30,
                                "records": [{"content": 'A "x"'}],
                            },
                            {
                                "name": "z.",
                                "type": "SOA",
                                "ttl": 3600,
                                "records": [{"content": "s"}],
                            },
                        ]
                    },
                )
            return httpx.Response(204)
        return httpx.Response(500, json={"error": "unexpected"})


def client(server: Server) -> HttpPowerDns:
    return HttpPowerDns(
        "http://pdns:8081/",
        "k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler)),
    )


async def test_a_zone_is_created_once_with_dotted_names() -> None:
    server = Server()
    dns = client(server)
    await dns.ensure_zone("z.example.test", ["ns1.example.test"])
    await dns.ensure_zone("z.example.test", ["ns1.example.test"])
    posts = [c for c in server.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2] == {
        "name": "z.example.test.",
        "kind": "Native",
        "nameservers": ["ns1.example.test."],
    }


async def test_record_sets_are_replaced_in_one_request_and_deleted_by_name() -> None:
    server = Server()
    dns = client(server)
    await dns.put_rrsets("z.", [Rrset("w.z.", "LUA", 30, ["A \"ifportup(443, {'1.1.1.1'})\""])])
    method, path, body = server.calls[-1]
    assert method == "PATCH" and path.endswith("/zones/z.")
    assert body == {
        "rrsets": [
            {
                "name": "w.z.",
                "type": "LUA",
                "ttl": 30,
                "changetype": "REPLACE",
                "records": [{"content": "A \"ifportup(443, {'1.1.1.1'})\"", "disabled": False}],
            }
        ]
    }
    await dns.delete_rrsets("z.", [("w.z.", "LUA")])
    assert server.calls[-1][2] == {
        "rrsets": [{"name": "w.z.", "type": "LUA", "changetype": "DELETE"}]
    }
    before = len(server.calls)
    await dns.put_rrsets("z.", [])
    await dns.delete_rrsets("z.", [])
    assert len(server.calls) == before


async def test_only_lua_record_sets_are_read_back() -> None:
    server = Server()
    server.zones.add("z.")
    got = await client(server).get_rrsets("z")
    assert got == [Rrset("w.z.", "LUA", 30, ['A "x"'])]


async def test_dnssec_returns_the_ds_of_active_signing_keys_only() -> None:
    server = Server()
    dns = client(server)
    assert await dns.enable_dnssec("z") == ["z. IN DS 1 13 2 aa"]
    assert server.calls[0][2] == {"dnssec": True, "api_rectify": True}


async def test_errors_become_one_type_with_a_short_message() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "Domain 'z.' is not a master"})

    dns = HttpPowerDns(
        "http://p", "k", client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )
    with pytest.raises(PowerDnsError, match="HTTP 422 Domain"):
        await dns.get_rrsets("z")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    dns = HttpPowerDns(
        "http://p", "k", client=httpx.AsyncClient(transport=httpx.MockTransport(down))
    )
    with pytest.raises(PowerDnsError, match="not reachable"):
        await dns.delete_zone("z")


async def test_the_fake_keeps_zones_and_can_fail() -> None:
    fake = FakePowerDns()
    await fake.ensure_zone("z.example.test", ["ns1.example.test"])
    await fake.put_rrsets("z.example.test", [Rrset("a.z.example.test.", "LUA", 5, ['A "x"'])])
    assert [r.name for r in await fake.get_rrsets("z.example.test")] == ["a.z.example.test."]
    assert await fake.ds_records("z.example.test") == []
    assert (await fake.enable_dnssec("z.example.test"))[0].startswith("z.example.test. IN DS")
    fake.fail = True
    with pytest.raises(PowerDnsError):
        await fake.get_rrsets("z.example.test")
    assert fqdn("a.b") == "a.b." and fqdn("a.b.") == "a.b."

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Address records for a virtual service on RFC 2136 and PowerDNS (ADR 0193, issue #19).

Both adapters are driven against in-memory fakes: a name server that takes signed updates and a
PowerDNS HTTP API. No network is touched.
"""

import json
from typing import Any

import dns.message
import dns.opcode
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rrset
import httpx
import pytest

from switchyard.adapters.cloudflare import HttpCloudflare
from switchyard.adapters.dns import base
from switchyard.adapters.dns.azure import AzureDns
from switchyard.adapters.dns.base import DnsError, address_target, check_zone_name
from switchyard.adapters.dns.google import GoogleDns
from switchyard.adapters.dns.powerdns import PowerDns
from switchyard.adapters.dns.rfc2136 import Rfc2136
from switchyard.adapters.dns.route53 import Route53

# -- the catalogue tells the truth about each provider ---------------------------------------

CLASSES: dict[str, type] = {
    "cloudflare": HttpCloudflare,
    "route53": Route53,
    "azure": AzureDns,
    "google": GoogleDns,
    "rfc2136": Rfc2136,
    "powerdns": PowerDns,
}


def test_only_the_providers_that_implement_address_records_declare_them() -> None:
    declared = {t.type for t in base.types() if t.supports_address_records}
    assert declared == {"cloudflare", "rfc2136", "powerdns"}
    assert {t.type for t in base.types()} - declared == {"route53", "azure", "google"}


def test_the_registry_flag_and_the_runtime_check_agree() -> None:
    """Neither can drift: the flag the API shows is what ``supports_addresses`` answers."""
    for spec in base.types():
        assert base.supports_addresses(CLASSES[spec.type]) is spec.supports_address_records, (
            spec.type
        )


# -- name and zone validation ----------------------------------------------------------------


def test_a_zone_must_be_a_concrete_dns_name() -> None:
    assert check_zone_name("Example.Test.") == "example.test"
    with pytest.raises(ValueError, match="wildcard"):
        check_zone_name("*.example.test")
    with pytest.raises(ValueError, match="invalid label"):
        check_zone_name("example..test")


def test_check_settings_reports_a_bad_zone() -> None:
    problems = base.check_settings(
        "powerdns",
        {"api_url": "https://pdns.example.test", "zone": "not a zone"},
        {"api_key": "k"},
    )
    assert problems["config.zone"].startswith("zone:")


def test_a_record_name_must_be_an_exact_name_inside_the_zone() -> None:
    assert address_target("App.Example.Test.", "192.0.2.20", "example.test") == (
        "app.example.test",
        "A",
        "192.0.2.20",
    )
    assert address_target("app.example.test", "2001:db8::1", "example.test")[1] == "AAAA"
    with pytest.raises(DnsError, match="not inside the zone"):
        address_target("app.other.test", "192.0.2.20", "example.test")
    with pytest.raises(DnsError, match="not an IP address"):
        address_target("app.example.test", "not-an-ip", "example.test")
    with pytest.raises(DnsError, match="invalid label"):
        address_target("*.example.test", "192.0.2.20", "example.test")


# -- PowerDNS --------------------------------------------------------------------------------


class FakePowerDnsApi:
    """An in-memory PowerDNS zone: record sets by ``(name, type)``, one PATCH per change."""

    def __init__(self, zone: str = "example.test") -> None:
        self.zone = zone
        self.rrsets: dict[tuple[str, str], tuple[int, list[str]]] = {}
        self.calls: list[tuple[str, dict[str, str], Any]] = []

    def seed(self, name: str, rtype: str, values: list[str], ttl: int = 300) -> None:
        self.rrsets[(name, rtype)] = (ttl, values)

    def values(self, name: str, rtype: str = "A") -> list[str]:
        return list(self.rrsets.get((name, rtype), (0, []))[1])

    def writes(self) -> list[Any]:
        return [body for method, _, body in self.calls if method == "PATCH"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        params = dict(request.url.params)
        self.calls.append((request.method, params, body))
        assert request.headers["x-api-key"] == "key"
        assert request.url.path == f"/api/v1/servers/localhost/zones/{self.zone}."
        if request.method == "GET":
            name, rtype = params["rrset_name"].rstrip("."), params["rrset_type"]
            found = self.rrsets.get((name, rtype))
            rrsets = (
                [
                    {
                        "name": f"{name}.",
                        "type": rtype,
                        "ttl": found[0],
                        "records": [{"content": v, "disabled": False} for v in found[1]],
                    }
                ]
                if found
                else []
            )
            return httpx.Response(200, json={"rrsets": rrsets})
        for rrset in (body or {}).get("rrsets", []):
            key = (rrset["name"].rstrip("."), rrset["type"])
            if rrset["changetype"] == "REPLACE":
                self.rrsets[key] = (rrset["ttl"], [r["content"] for r in rrset["records"]])
            else:
                self.rrsets.pop(key, None)
        return httpx.Response(204)


def powerdns(api: FakePowerDnsApi) -> PowerDns:
    return PowerDns(
        "https://pdns.example.test:8081",
        "key",
        api.zone,
        transport=httpx.MockTransport(api.handler),
    )


async def test_powerdns_creates_updates_and_deletes_an_address_record() -> None:
    api = FakePowerDnsApi()
    dns = powerdns(api)
    handle = await dns.upsert_address(
        "app.example.test", "192.0.2.20", ttl=300, proxied=False, handle=None
    )
    assert handle == "192.0.2.20", "the handle is the record's value"
    assert api.rrsets[("app.example.test", "A")] == (300, ["192.0.2.20"])
    assert api.writes()[-1]["rrsets"][0]["changetype"] == "REPLACE"

    handle = await dns.upsert_address(
        "app.example.test", "192.0.2.21", ttl=120, proxied=False, handle=handle
    )
    assert handle == "192.0.2.21"
    assert api.rrsets[("app.example.test", "A")] == (120, ["192.0.2.21"])

    await dns.delete_address("app.example.test", handle)
    assert api.values("app.example.test") == []
    assert api.writes()[-1]["rrsets"][0]["changetype"] == "DELETE"
    writes = len(api.writes())
    await dns.delete_address("app.example.test", handle)
    assert len(api.writes()) == writes, "deleting twice writes nothing"


async def test_powerdns_keeps_a_value_it_did_not_create_next_to_its_own() -> None:
    api = FakePowerDnsApi()
    api.seed("app.example.test", "A", ["198.51.100.9", "192.0.2.20"])
    dns = powerdns(api)
    await dns.upsert_address(
        "app.example.test", "192.0.2.21", ttl=300, proxied=False, handle="192.0.2.20"
    )
    assert api.values("app.example.test") == ["198.51.100.9", "192.0.2.21"]
    await dns.delete_address("app.example.test", "192.0.2.21")
    assert api.values("app.example.test") == ["198.51.100.9"], "a foreign value survives"


async def test_powerdns_refuses_to_take_over_a_foreign_record() -> None:
    api = FakePowerDnsApi()
    api.seed("app.example.test", "A", ["198.51.100.9"])
    with pytest.raises(DnsError, match="did not create"):
        await powerdns(api).upsert_address(
            "app.example.test", "192.0.2.20", ttl=300, proxied=False, handle=None
        )
    assert api.values("app.example.test") == ["198.51.100.9"]
    assert api.writes() == []


async def test_powerdns_adopts_an_identical_record_without_writing() -> None:
    api = FakePowerDnsApi()
    api.seed("app.example.test", "A", ["192.0.2.20"])
    handle = await powerdns(api).upsert_address(
        "app.example.test", "192.0.2.20", ttl=300, proxied=False, handle=None
    )
    assert handle == "192.0.2.20"
    assert api.writes() == []


async def test_powerdns_recreates_its_record_after_someone_deletes_it_by_hand() -> None:
    api = FakePowerDnsApi()
    await powerdns(api).upsert_address(
        "app.example.test", "192.0.2.20", ttl=300, proxied=False, handle="192.0.2.20"
    )
    assert api.values("app.example.test") == ["192.0.2.20"]


async def test_powerdns_refuses_a_proxied_record_and_a_name_outside_the_zone() -> None:
    dns = powerdns(FakePowerDnsApi())
    with pytest.raises(DnsError, match="no proxy"):
        await dns.upsert_address(
            "app.example.test", "192.0.2.20", ttl=300, proxied=True, handle=None
        )
    with pytest.raises(DnsError, match="not inside the zone"):
        await dns.upsert_address(
            "app.other.test", "192.0.2.20", ttl=300, proxied=False, handle=None
        )


# -- RFC 2136 --------------------------------------------------------------------------------


class FakeUpdateServer:
    """An in-memory name server for one zone: answers queries, applies signed updates."""

    def __init__(self, zone: str = "example.test") -> None:
        self.zone = zone
        self.records: dict[tuple[str, str], tuple[int, list[str]]] = {}
        self.queries: list[dns.message.Message] = []
        self.updates: list[dns.message.Message] = []
        self.rcode = dns.rcode.NOERROR
        self.query_rcode = dns.rcode.NOERROR

    def seed(self, name: str, rtype: str, values: list[str], ttl: int = 300) -> None:
        self.records[(name, rtype)] = (ttl, values)

    def values(self, name: str, rtype: str = "A") -> list[str]:
        return list(self.records.get((name, rtype), (0, []))[1])

    def __call__(
        self,
        message: dns.message.Message,
        where: str,
        port: int = 53,
        timeout: float | None = None,
    ) -> dns.message.Message:
        assert (where, port) == ("198.51.100.7", 5353)
        if message.opcode() == dns.opcode.QUERY:
            self.queries.append(message)
            return self._answer(message)
        self.updates.append(message)
        self._apply(message)
        response = dns.message.make_response(message)
        response.set_rcode(self.rcode)
        return response

    def _answer(self, message: dns.message.Message) -> dns.message.Message:
        question = message.question[0]
        name = str(question.name).rstrip(".")
        rtype = dns.rdatatype.to_text(question.rdtype)
        response = dns.message.make_response(message)
        ttl, values = self.records.get((name, rtype), (0, []))
        if values:
            response.answer.append(dns.rrset.from_text(f"{name}.", ttl, "IN", rtype, *values))
        else:
            response.set_rcode(self.query_rcode)
        return response

    def _apply(self, message: dns.message.Message) -> None:
        """Apply one update message: delete our own value, add the new one."""
        for rrset in message.authority:
            key = (str(rrset.name).rstrip("."), dns.rdatatype.to_text(rrset.rdtype))
            ttl, values = self.records.get(key, (0, []))
            fresh = [str(r) for r in rrset]
            if rrset.deleting:
                remaining = [v for v in values if v not in fresh]
                if remaining:
                    self.records[key] = (ttl, remaining)
                else:
                    self.records.pop(key, None)
            else:
                self.records[key] = (
                    int(rrset.ttl),
                    [*values, *(v for v in fresh if v not in values)],
                )


def server_for(monkeypatch: pytest.MonkeyPatch, server: FakeUpdateServer) -> Rfc2136:
    """Patch the transport with the fake and return the client."""
    monkeypatch.setattr("dns.query.tcp", server)
    return Rfc2136("198.51.100.7", "example.test", "acme-key", "c2VjcmV0c2VjcmV0", port=5353)


async def test_rfc2136_creates_updates_and_deletes_an_address_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeUpdateServer()
    dns = server_for(monkeypatch, server)
    handle = await dns.upsert_address(
        "app.example.test", "192.0.2.20", ttl=300, proxied=False, handle=None
    )
    assert handle == "192.0.2.20"
    assert server.records[("app.example.test", "A")] == (300, ["192.0.2.20"])
    assert server.queries[-1].had_tsig, "the read-back query is signed too"

    handle = await dns.upsert_address(
        "app.example.test", "192.0.2.21", ttl=120, proxied=False, handle=handle
    )
    assert handle == "192.0.2.21"
    assert server.records[("app.example.test", "A")] == (120, ["192.0.2.21"])
    assert any(rr.deleting for rr in server.updates[-1].authority), "the old value is removed"

    await dns.delete_address("app.example.test", handle)
    assert server.values("app.example.test") == []
    sent = len(server.updates)
    await dns.delete_address("app.example.test", handle)
    assert len(server.updates) == sent, "deleting twice sends nothing"


async def test_rfc2136_publishes_the_aaaa_record_of_an_ipv6_vip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeUpdateServer()
    handle = await server_for(monkeypatch, server).upsert_address(
        "app.example.test", "2001:db8::20", ttl=300, proxied=False, handle=None
    )
    assert handle == "2001:db8::20"
    assert server.values("app.example.test", "AAAA") == ["2001:db8::20"]


async def test_rfc2136_refuses_to_take_over_a_foreign_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeUpdateServer()
    server.seed("app.example.test", "A", ["198.51.100.9"])
    with pytest.raises(DnsError, match="did not create"):
        await server_for(monkeypatch, server).upsert_address(
            "app.example.test", "192.0.2.20", ttl=300, proxied=False, handle=None
        )
    assert server.values("app.example.test") == ["198.51.100.9"]
    assert server.updates == [], "nothing is written on a conflict"


async def test_rfc2136_adopts_an_identical_record_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeUpdateServer()
    server.seed("app.example.test", "A", ["192.0.2.20"])
    handle = await server_for(monkeypatch, server).upsert_address(
        "app.example.test", "192.0.2.20", ttl=300, proxied=False, handle=None
    )
    assert handle == "192.0.2.20"
    assert server.updates == []


async def test_rfc2136_recovers_when_its_record_was_deleted_by_hand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeUpdateServer()
    handle = await server_for(monkeypatch, server).upsert_address(
        "app.example.test", "192.0.2.20", ttl=300, proxied=False, handle="192.0.2.20"
    )
    assert handle == "192.0.2.20"
    assert server.values("app.example.test") == ["192.0.2.20"]


async def test_rfc2136_refuses_a_proxied_record_a_foreign_name_and_a_bad_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns = server_for(monkeypatch, FakeUpdateServer())
    with pytest.raises(DnsError, match="no proxy"):
        await dns.upsert_address(
            "app.example.test", "192.0.2.20", ttl=300, proxied=True, handle=None
        )
    with pytest.raises(DnsError, match="not inside the zone"):
        await dns.upsert_address(
            "app.other.test", "192.0.2.20", ttl=300, proxied=False, handle=None
        )
    with pytest.raises(DnsError, match="not an IP address"):
        await dns.upsert_address("app.example.test", "x", ttl=300, proxied=False, handle=None)


async def test_rfc2136_reports_a_refused_query(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeUpdateServer()
    server.query_rcode = dns.rcode.REFUSED
    with pytest.raises(DnsError, match="REFUSED"):
        await server_for(monkeypatch, server).upsert_address(
            "app.example.test", "192.0.2.20", ttl=300, proxied=False, handle=None
        )


async def test_rfc2136_reports_a_refused_update(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeUpdateServer()
    server.rcode = dns.rcode.NOTAUTH
    with pytest.raises(DnsError, match="NOTAUTH"):
        await server_for(monkeypatch, server).upsert_address(
            "app.example.test", "192.0.2.20", ttl=300, proxied=False, handle=None
        )

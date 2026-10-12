"""The DNS providers against mocked APIs: several values per name, cleanup, errors."""

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import dns.message
import dns.opcode
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.tsig
import httpx
import pytest

from switchyard.adapters.dns import base
from switchyard.adapters.dns.azure import AzureDns
from switchyard.adapters.dns.base import DnsError, check_endpoint, check_settings
from switchyard.adapters.dns.factory import build_provider
from switchyard.adapters.dns.powerdns import PowerDns
from switchyard.adapters.dns.rfc2136 import Rfc2136
from switchyard.adapters.dns.route53 import AwsCredentials, Route53

NAME = "_acme-challenge.example.test"


def transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# -- catalogue and policy --------------------------------------------------------------------


def test_every_type_declares_its_secret_fields() -> None:
    for spec in base.types():
        assert any(f.secret for f in spec.fields), spec.type
        assert len({f.key for f in spec.fields}) == len(spec.fields)


def test_settings_are_checked_per_field() -> None:
    assert check_settings("cloudflare", {}, {"api_token": "t"}) == {}
    problems = check_settings("azure", {"cloud": "mars", "typo": "x"}, {})
    assert problems["config.cloud"].startswith("must be one of")
    assert problems["config.typo"] == "not a setting of this provider"
    assert problems["credentials.client_secret"] == "required"
    assert "type" in check_settings("nope", {}, {})
    # changing only the settings does not need the secrets again
    assert "credentials.api_key" not in check_settings(
        "powerdns",
        {"api_url": "https://p.example.test", "zone": "example.test"},
        {},
        secrets_required=False,
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "169.254.169.254", "0.0.0.0", "224.0.0.1"])  # noqa: S104
def test_endpoints_never_reach_loopback_or_metadata(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWITCHYARD_ENGINE_POLICY", "strict")
    with pytest.raises(ValueError, match="may contact"):
        check_endpoint(host, "server")


def test_private_endpoints_need_the_operator_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWITCHYARD_ENGINE_POLICY", "strict")
    monkeypatch.delenv("SWITCHYARD_DNS_CIDRS", raising=False)
    with pytest.raises(ValueError, match="may contact"):
        check_endpoint("10.0.0.53", "server")
    monkeypatch.setenv("SWITCHYARD_DNS_CIDRS", "10.0.0.0/24")
    check_endpoint("10.0.0.53", "server")
    with pytest.raises(ValueError, match="may contact"):
        check_endpoint("10.1.0.53", "server")
    check_endpoint("8.8.8.8", "server")


def test_the_factory_refuses_servers_the_policy_forbids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWITCHYARD_ENGINE_POLICY", "strict")
    with pytest.raises(DnsError, match="may contact"):
        build_provider(
            "powerdns",
            {"api_url": "http://169.254.169.254:8081", "zone": "example.test"},
            {"api_key": "k"},
        )
    with pytest.raises(DnsError, match="required"):
        build_provider("cloudflare", {}, {})


# -- Route 53 --------------------------------------------------------------------------------

R53 = "https://route53.amazonaws.com/doc/2013-04-01/"


class Route53Api:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.changes: list[tuple[str, list[str]]] = []
        self.auth: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("authorization", ""))
        path = request.url.path
        if path.endswith("/hostedzonesbyname"):
            zones = (
                "<HostedZone><Id>/hostedzone/ZPRIV</Id><Name>example.test.</Name>"
                "<Config><PrivateZone>true</PrivateZone></Config></HostedZone>"
                "<HostedZone><Id>/hostedzone/ZPUB</Id><Name>example.test.</Name>"
                "<Config><PrivateZone>false</PrivateZone></Config></HostedZone>"
            )
            return httpx.Response(
                200,
                text=f'<ListHostedZonesByNameResponse xmlns="{R53}"><HostedZones>{zones}</HostedZones></ListHostedZonesByNameResponse>',
            )
        if request.method == "GET" and path.endswith("/rrset"):
            records = "".join(
                f"<ResourceRecord><Value>{v.replace(chr(34), '&quot;')}</Value></ResourceRecord>"
                for v in self.values
            )
            sets = (
                f"<ResourceRecordSet><Name>{NAME}.</Name><Type>TXT</Type><TTL>60</TTL>"
                f"<ResourceRecords>{records}</ResourceRecords></ResourceRecordSet>"
                if self.values
                else ""
            )
            return httpx.Response(
                200,
                text=f'<ListResourceRecordSetsResponse xmlns="{R53}"><ResourceRecordSets>{sets}</ResourceRecordSets></ListResourceRecordSetsResponse>',
            )
        if request.method == "POST" and path.endswith("/rrset"):
            body = request.content.decode()
            action = body.split("<Action>")[1].split("</Action>")[0]
            values = [
                v.split("</Value>")[0].replace("&quot;", '"') for v in body.split("<Value>")[1:]
            ]
            self.changes.append((action, values))
            self.values = [] if action == "DELETE" else values
            return httpx.Response(
                200,
                text=f'<ChangeResourceRecordSetsResponse xmlns="{R53}"><ChangeInfo><Id>/change/C1</Id><Status>PENDING</Status></ChangeInfo></ChangeResourceRecordSetsResponse>',
            )
        if "/change/" in path:
            return httpx.Response(
                200,
                text=f'<GetChangeResponse xmlns="{R53}"><ChangeInfo><Id>/change/C1</Id><Status>INSYNC</Status></ChangeInfo></GetChangeResponse>',
            )
        return httpx.Response(403, text="<Error><Message>denied</Message></Error>")


def route53(api: Route53Api, **kwargs: Any) -> Route53:
    return Route53(
        AwsCredentials("AKIDEXAMPLE", "secret"),
        transport=transport(api.handler),
        poll_s=0,
        **kwargs,
    )


async def test_route53_keeps_both_challenge_values_and_removes_only_its_own() -> None:
    api = Route53Api()
    dns = route53(api)
    first = await dns.create_txt(NAME, "token-one")
    second = await dns.create_txt(NAME, "token-two")
    assert api.values == ['"token-one"', '"token-two"']
    await dns.delete_txt(NAME, first)
    assert api.values == ['"token-two"']
    await dns.delete_txt(NAME, second)
    assert api.values == []
    assert [a for a, _ in api.changes] == ["UPSERT", "UPSERT", "UPSERT", "DELETE"]
    await dns.delete_txt(NAME, second)  # idempotent
    assert all(a.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/") for a in api.auth)
    await dns.close()


async def test_route53_picks_the_public_zone_and_reports_errors() -> None:
    api = Route53Api()
    dns = route53(api, hosted_zone_id=None)
    await dns.create_txt(NAME, "v")
    # a failing call names the status and the AWS message
    bad = Route53(
        AwsCredentials("a", "b"),
        transport=transport(
            lambda r: httpx.Response(403, text="<Error><Message>denied</Message></Error>")
        ),
    )
    with pytest.raises(DnsError, match="HTTP 403: denied"):
        await bad.create_txt(NAME, "v")


async def test_route53_can_assume_a_role() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sts.amazonaws.com":
            form = parse_qs(request.content.decode())
            assert form["RoleArn"] == ["arn:aws:iam::1:role/dns"]
            return httpx.Response(
                200,
                text=(
                    '<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult>'
                    "<Credentials><AccessKeyId>ASIATEMP</AccessKeyId><SecretAccessKey>s2</SecretAccessKey>"
                    "<SessionToken>tok</SessionToken></Credentials></AssumeRoleResult></AssumeRoleResponse>"
                ),
            )
        seen.append(request.headers["authorization"])
        assert request.headers["x-amz-security-token"] == "tok"
        return Route53Api().handler(request)

    dns = Route53(
        AwsCredentials("AKID", "s"),
        hosted_zone_id="ZPUB",
        role_arn="arn:aws:iam::1:role/dns",
        transport=transport(handler),
        poll_s=0,
    )
    await dns.create_txt(NAME, "v")
    assert seen and all("Credential=ASIATEMP/" in a for a in seen)


# -- Azure DNS -------------------------------------------------------------------------------


class AzureApi:
    def __init__(self) -> None:
        self.values: list[str] | None = None
        self.token_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            self.token_calls += 1
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["client_credentials"]
            assert request.url.path == "/tenant-1/oauth2/v2.0/token"
            return httpx.Response(200, json={"access_token": "bearer", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer bearer"
        assert request.url.path.endswith("/dnsZones/example.test/TXT/_acme-challenge")
        if request.method == "GET":
            if self.values is None:
                return httpx.Response(404, json={"error": {"code": "NotFound"}})
            records = [{"value": [v]} for v in self.values]
            return httpx.Response(200, json={"properties": {"TTL": 60, "TXTRecords": records}})
        if request.method == "PUT":
            body = json.loads(request.content)
            self.values = [v for r in body["properties"]["TXTRecords"] for v in r["value"]]
            return httpx.Response(200, json={})
        if request.method == "DELETE":
            self.values = None
            return httpx.Response(200)
        return httpx.Response(405)


def azure(api: AzureApi) -> AzureDns:
    return AzureDns(
        azure_tenant_id="tenant-1",
        client_id="client",
        client_secret="s",
        subscription_id="sub",
        resource_group="rg",
        zone="example.test",
        transport=transport(api.handler),
    )


async def test_azure_adds_and_removes_single_values() -> None:
    api = AzureApi()
    dns = azure(api)
    a = await dns.create_txt(NAME, "one")
    b = await dns.create_txt(NAME, "two")
    assert api.values == ["one", "two"]
    await dns.delete_txt(NAME, a)
    assert api.values == ["two"]
    await dns.delete_txt(NAME, b)
    assert api.values is None
    await dns.delete_txt(NAME, b)
    assert api.token_calls == 1, "the token is reused"


async def test_azure_refuses_names_outside_its_zone_and_reports_sign_in_failures() -> None:
    dns = azure(AzureApi())
    with pytest.raises(DnsError, match="not inside the Azure zone"):
        await dns.create_txt("_acme-challenge.other.test", "v")
    failing = AzureDns(
        azure_tenant_id="t",
        client_id="c",
        client_secret="bad",
        subscription_id="s",
        resource_group="r",
        zone="example.test",
        transport=transport(
            lambda r: httpx.Response(
                401, json={"error": "invalid_client", "error_description": "wrong secret"}
            )
        ),
    )
    with pytest.raises(DnsError, match="wrong secret"):
        await failing.create_txt(NAME, "v")


# -- PowerDNS --------------------------------------------------------------------------------


async def test_powerdns_replaces_the_record_set() -> None:
    state: dict[str, Any] = {"records": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "key"
        assert request.url.path == "/api/v1/servers/localhost/zones/example.test."
        if request.method == "GET":
            rrsets = (
                [{"name": f"{NAME}.", "type": "TXT", "ttl": 60, "records": state["records"]}]
                if state["records"]
                else []
            )
            return httpx.Response(200, json={"rrsets": rrsets})
        rr = json.loads(request.content)["rrsets"][0]
        state["records"] = rr.get("records", []) if rr["changetype"] == "REPLACE" else []
        state["last"] = rr["changetype"]
        return httpx.Response(204)

    dns = PowerDns(
        "https://pdns.example.test:8081", "key", "example.test", transport=transport(handler)
    )
    a = await dns.create_txt(NAME, "one")
    b = await dns.create_txt(NAME, "two")
    assert [r["content"] for r in state["records"]] == ['"one"', '"two"']
    await dns.delete_txt(NAME, a)
    assert [r["content"] for r in state["records"]] == ['"two"']
    await dns.delete_txt(NAME, b)
    assert state["last"] == "DELETE"
    with pytest.raises(DnsError, match="not inside"):
        await dns.create_txt("x.other.test", "v")


# -- RFC 2136 --------------------------------------------------------------------------------


async def test_rfc2136_sends_signed_updates_that_add_and_remove_single_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dns.message.Message] = []
    rcode = {"value": dns.rcode.NOERROR}

    def tcp(
        message: dns.message.Message, where: str, port: int = 53, timeout: float | None = None
    ) -> Any:
        assert (where, port) == ("198.51.100.7", 5353)
        sent.append(message)
        response = dns.message.make_response(message)
        response.set_rcode(rcode["value"])
        return response

    monkeypatch.setattr("dns.query.tcp", tcp)
    client = Rfc2136("198.51.100.7", "example.test", "acme-key", "c2VjcmV0c2VjcmV0", port=5353)
    handle = await client.create_txt(NAME, "tok")
    await client.delete_txt(NAME, handle)
    add, remove = sent
    assert add.opcode() == dns.opcode.UPDATE
    assert add.had_tsig and remove.had_tsig, "updates are signed"
    assert str(add.question[0].name) == "example.test."
    assert [str(r) for rr in add.authority for r in rr] == ['"tok"']
    removed = remove.authority[0]
    assert removed.deleting == dns.rdataclass.NONE, "only the one value is deleted, not the set"
    rcode["value"] = dns.rcode.REFUSED
    with pytest.raises(DnsError, match="REFUSED"):
        await client.create_txt(NAME, "x")
    with pytest.raises(DnsError, match="not inside"):
        await client.create_txt("x.other.test", "v")

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Amazon Route 53: TXT records through the REST API, signed with AWS Signature Version 4.

No AWS SDK: the API is small and the signing is a page of standard-library code. A record set holds
all values of a name, so adding or removing one value reads the set first. Temporary credentials
(a session token) and role assumption through STS are supported.
"""

import asyncio
import datetime as dt
import hashlib
import hmac
import time
import xml.etree.ElementTree as ET  # types only; parsing is defusedxml
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from defusedxml import ElementTree as SafeET

from switchyard.adapters.dns.base import DnsError
from switchyard.adapters.dns.zones import zone_candidates

HOST = "route53.amazonaws.com"
STS_HOST = "sts.amazonaws.com"
REGION = "us-east-1"
VERSION = "2013-04-01"
_NS = {"r": f"https://route53.amazonaws.com/doc/{VERSION}/"}


@dataclass(frozen=True)
class AwsCredentials:
    """Access keys, with a session token for temporary ones."""

    access_key_id: str
    secret_access_key: str
    session_token: str | None = None


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def sign(
    method: str,
    host: str,
    path: str,
    query: dict[str, str],
    body: bytes,
    credentials: AwsCredentials,
    *,
    service: str,
    now: dt.datetime,
    region: str = REGION,
    s3: bool = False,
) -> dict[str, str]:
    """AWS Signature Version 4 headers for one request.

    Example:
        The AWS documentation's "get-vanilla" test vector:

        >>> creds = AwsCredentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
        >>> h = sign("GET", "example.amazonaws.com", "/", {}, b"", creds, service="service",
        ...          now=dt.datetime(2015, 8, 30, 12, 36, tzinfo=dt.UTC))
        >>> h["Authorization"].rsplit("Signature=", 1)[1]
        '5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31'
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {"host": host, "x-amz-date": amz_date}
    if s3:
        headers["x-amz-content-sha256"] = payload_hash
    if credentials.session_token:
        headers["x-amz-security-token"] = credentials.session_token
    signed = ";".join(sorted(headers))
    canonical_query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query.items())
    )
    canonical = "\n".join(
        [
            method,
            quote(path, safe="/-_.~"),
            canonical_query,
            "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)),
            signed,
            payload_hash,
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = _hmac(
        _hmac(_hmac(_hmac(f"AWS4{credentials.secret_access_key}".encode(), day), region), service),
        "aws4_request",
    )
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {
        "x-amz-date": amz_date,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={credentials.access_key_id}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        ),
    }
    if s3:
        out["x-amz-content-sha256"] = payload_hash
    if credentials.session_token:
        out["x-amz-security-token"] = credentials.session_token
    return out


def _quote_txt(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Route53:
    """Route 53 TXT records for ACME DNS-01."""

    def __init__(
        self,
        credentials: AwsCredentials,
        *,
        hosted_zone_id: str | None = None,
        role_arn: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sync_timeout_s: float = 120.0,
        poll_s: float = 3.0,
    ) -> None:
        """Create the client.

        Args:
            credentials: Access keys.
            hosted_zone_id: A fixed zone; otherwise the public zone of each name is found.
            role_arn: An IAM role to assume with the credentials first.
            transport: Replaceable HTTP transport (tests).
            sync_timeout_s: How long to wait for a change to reach every Route 53 server.
            poll_s: Pause between checks of that.
        """
        self._base = credentials
        self._zone = hosted_zone_id
        self._role = role_arn
        self._http = httpx.AsyncClient(transport=transport, timeout=30.0)
        self._sync_timeout = sync_timeout_s
        self._poll = poll_s
        self._active: AwsCredentials | None = None
        self._expires = 0.0
        self._zones: dict[str, str] = {}

    def _now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    async def _credentials(self) -> AwsCredentials:
        if self._role is None:
            return self._base
        if self._active is not None and time.time() < self._expires - 60:
            return self._active
        form = {
            "Action": "AssumeRole",
            "Version": "2011-06-15",
            "RoleArn": self._role,
            "RoleSessionName": "switchyard-acme",
            "DurationSeconds": "900",
        }
        body = "&".join(f"{k}={quote(v, safe='')}" for k, v in form.items()).encode()
        headers = sign("POST", STS_HOST, "/", {}, body, self._base, service="sts", now=self._now())
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = await self._http.post(f"https://{STS_HOST}/", content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise DnsError(f"STS AssumeRole: {exc}") from exc
        if response.status_code != 200:
            raise DnsError(f"STS AssumeRole: HTTP {response.status_code}: {_error(response)}")
        root = SafeET.fromstring(response.text)
        ns = {"s": root.tag.partition("}")[0].strip("{")}
        node = root.find(".//s:Credentials", ns)
        if node is None:
            raise DnsError("STS AssumeRole: no credentials in the answer")

        def text(tag: str) -> str:
            found = node.find(f"s:{tag}", ns)
            return found.text or "" if found is not None else ""

        self._active = AwsCredentials(
            text("AccessKeyId"), text("SecretAccessKey"), text("SessionToken")
        )
        self._expires = time.time() + 900
        return self._active

    async def _call(
        self, method: str, path: str, *, query: dict[str, str] | None = None, body: str = ""
    ) -> ET.Element:
        creds = await self._credentials()
        data = body.encode()
        headers = sign(
            method, HOST, path, query or {}, data, creds, service="route53", now=self._now()
        )
        if data:
            headers["Content-Type"] = "text/xml"
        try:
            response = await self._http.request(
                method, f"https://{HOST}{path}", params=query, content=data or None, headers=headers
            )
        except httpx.HTTPError as exc:
            raise DnsError(f"Route 53 {method} {path}: {exc}") from exc
        if response.status_code >= 400:
            raise DnsError(
                f"Route 53 {method} {path}: HTTP {response.status_code}: {_error(response)}"
            )
        root: ET.Element = SafeET.fromstring(response.text)
        return root

    async def _zone_id(self, name: str) -> str:
        if self._zone:
            return self._zone
        for candidate in zone_candidates(name):
            if candidate in self._zones:
                return self._zones[candidate]
            root = await self._call(
                "GET",
                f"/{VERSION}/hostedzonesbyname",
                query={"dnsname": candidate, "maxitems": "5"},
            )
            for zone in root.iterfind(".//r:HostedZone", _NS):
                found = (zone.findtext("r:Name", "", _NS) or "").rstrip(".")
                private = zone.findtext("r:Config/r:PrivateZone", "false", _NS) == "true"
                if found == candidate and not private:
                    zone_id = (zone.findtext("r:Id", "", _NS) or "").rpartition("/")[2]
                    self._zones[candidate] = zone_id
                    return zone_id
        raise DnsError(f"no public Route 53 hosted zone found for {name}")

    async def _values(self, zone: str, name: str) -> tuple[int, list[str]]:
        root = await self._call(
            "GET",
            f"/{VERSION}/hostedzone/{zone}/rrset",
            query={"name": f"{name}.", "type": "TXT", "maxitems": "1"},
        )
        for rrset in root.iterfind(".//r:ResourceRecordSet", _NS):
            same = (rrset.findtext("r:Name", "", _NS) or "").rstrip(".").lower() == name.lower()
            if same and rrset.findtext("r:Type", "", _NS) == "TXT":
                ttl = int(rrset.findtext("r:TTL", "60", _NS) or 60)
                values = [v.text or "" for v in rrset.iterfind(".//r:ResourceRecord/r:Value", _NS)]
                return ttl, values
        return 60, []

    async def _change(self, zone: str, action: str, name: str, ttl: int, values: list[str]) -> None:
        records = "".join(
            f"<ResourceRecord><Value>{_xml(v)}</Value></ResourceRecord>" for v in values
        )
        body = (
            f'<ChangeResourceRecordSetsRequest xmlns="{_NS["r"]}"><ChangeBatch><Changes><Change>'
            f"<Action>{action}</Action><ResourceRecordSet><Name>{_xml(name)}.</Name><Type>TXT</Type>"
            f"<TTL>{ttl}</TTL><ResourceRecords>{records}</ResourceRecords></ResourceRecordSet>"
            "</Change></Changes></ChangeBatch></ChangeResourceRecordSetsRequest>"
        )
        root = await self._call("POST", f"/{VERSION}/hostedzone/{zone}/rrset", body=body)
        change = (root.findtext(".//r:ChangeInfo/r:Id", "", _NS) or "").rpartition("/")[2]
        if change:
            await self._in_sync(change)

    async def _in_sync(self, change: str) -> None:
        deadline = time.monotonic() + self._sync_timeout
        while True:
            root = await self._call("GET", f"/{VERSION}/change/{change}")
            if root.findtext(".//r:ChangeInfo/r:Status", "", _NS) == "INSYNC":
                return
            if time.monotonic() > deadline:
                return  # the record exists; the ACME wait and the CA's retries cover the rest
            await asyncio.sleep(self._poll)

    async def create_txt(self, name: str, value: str, *, ttl: int = 60) -> str:
        """Add ``value`` to the TXT set of ``name``."""
        zone = await self._zone_id(name)
        _, values = await self._values(zone, name)
        quoted = _quote_txt(value)
        if quoted not in values:
            await self._change(zone, "UPSERT", name, max(ttl, 60), [*values, quoted])
        return value

    async def delete_txt(self, name: str, handle: str) -> None:
        """Remove ``handle`` (the value) from the TXT set of ``name``."""
        zone = await self._zone_id(name)
        ttl, values = await self._values(zone, name)
        quoted = _quote_txt(handle)
        if quoted not in values:
            return
        remaining = [v for v in values if v != quoted]
        if remaining:
            await self._change(zone, "UPSERT", name, ttl, remaining)
        else:
            await self._change(zone, "DELETE", name, ttl, values)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _error(response: httpx.Response) -> str:
    try:
        root = SafeET.fromstring(response.text)
        message = root.findtext(".//{*}Message") or root.findtext(".//Message")
        return message or response.text[:200]
    except (ET.ParseError, ValueError):  # ValueError: defusedxml refused a DTD or entity
        return response.text[:200]

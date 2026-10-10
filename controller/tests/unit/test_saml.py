"""SAML response validation: signed responses pass, every kind of wrong one is refused."""

import base64
import zlib
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from lxml import etree

from switchyard.services.saml import (
    SamlConfig,
    SamlError,
    authn_request_url,
    new_request_id,
    sp_metadata,
    verify_response,
)
from tests.saml_idp import ACS, IDP, NOW, SP, encode, make_idp, response_xml, sign

IDP_KEY = make_idp()
OTHER_KEY = make_idp("evil.example.test")
RID = "_req1"


def cfg(**kw: object) -> SamlConfig:
    base = {
        "id": "1",
        "name": "idp",
        "idp_entity_id": IDP,
        "sso_url": "https://idp.example.test/sso",
        "signing_certificates": (IDP_KEY.cert_pem,),
        "sp_entity_id": SP,
    }
    return SamlConfig(**{**base, **kw})  # type: ignore[arg-type]


def check(xml: str, **kw: object) -> object:
    return verify_response(cfg(**kw), encode(xml), acs_url=ACS, request_id=RID, now=NOW)


def test_a_signed_assertion_is_accepted() -> None:
    result = check(sign(response_xml(RID), IDP_KEY))
    assert result.subject == "alice-id"  # type: ignore[attr-defined]
    assert result.attributes["groups"] == ["sy-admins"]  # type: ignore[attr-defined]
    assert result.assertion_id == "_assertion1"  # type: ignore[attr-defined]


def test_a_signed_response_is_accepted() -> None:
    result = check(sign(response_xml(RID), IDP_KEY, what="response"))
    assert result.first("email") == "alice@example.test"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        (response_xml(RID), "unsigned"),
        (sign(response_xml(RID), OTHER_KEY), "unsigned or its signature"),
        (sign(response_xml(RID, in_response_to="_other"), IDP_KEY), "different login request"),
        (sign(response_xml(RID, audience="https://other.test"), IDP_KEY), "another audience"),
        (sign(response_xml(RID, issuer="https://evil.test"), IDP_KEY), "another identity provider"),
        (sign(response_xml(RID, recipient="https://evil.test/acs"), IDP_KEY), "addressed to"),
        (
            sign(response_xml(RID, not_on_or_after=NOW - timedelta(minutes=5)), IDP_KEY),
            "expired",
        ),
        (
            sign(response_xml(RID, not_before=NOW + timedelta(minutes=5)), IDP_KEY),
            "not valid yet",
        ),
        (
            sign(response_xml(RID, destination="https://evil.test/acs"), IDP_KEY, what="response"),
            "another service provider",
        ),
        (
            sign(
                response_xml(RID, status="urn:oasis:names:tc:SAML:2.0:status:Responder"),
                IDP_KEY,
                what="response",
            ),
            "failure",
        ),
    ],
)
def test_wrong_responses_are_refused(xml: str, message: str) -> None:
    with pytest.raises(SamlError, match=message):
        check(xml)


def test_a_tampered_assertion_is_refused() -> None:
    signed = sign(response_xml(RID), IDP_KEY).replace("alice-id", "mallory")
    with pytest.raises(SamlError):
        check(signed)


def test_signature_wrapping_uses_only_the_signed_assertion() -> None:
    signed = etree.fromstring(sign(response_xml(RID), IDP_KEY).encode())
    evil = etree.fromstring(
        response_xml(RID, assertion_id="_evil", subject="mallory", groups=("sy-admins",)).encode()
    )
    evil_assertion = evil.find("{urn:oasis:names:tc:SAML:2.0:assertion}Assertion")
    signed.insert(0, evil_assertion)  # unsigned assertion first, signed one after
    result = check(etree.tostring(signed).decode())
    assert result.subject == "alice-id"  # type: ignore[attr-defined]  # the signed one, never "mallory"


def test_dtd_garbage_and_missing_certificates_are_refused() -> None:
    with pytest.raises(SamlError, match="DTD"):
        check('<!DOCTYPE x [<!ENTITY a "b">]><x/>')
    with pytest.raises(SamlError, match=r"not valid base64|empty"):
        verify_response(cfg(), "", acs_url=ACS, request_id=RID, now=NOW)
    with pytest.raises(SamlError, match="no signing certificate"):
        check(sign(response_xml(RID), IDP_KEY), signing_certificates=())


def test_a_second_certificate_can_verify() -> None:
    result = check(
        sign(response_xml(RID), IDP_KEY),
        signing_certificates=(OTHER_KEY.cert_pem, IDP_KEY.cert_pem),
    )
    assert result.subject == "alice-id"  # type: ignore[attr-defined]


def test_authn_request_and_metadata() -> None:
    rid = new_request_id()
    url = authn_request_url(cfg(), acs_url=ACS, request_id=rid, relay_state="state1", now=NOW)
    query = parse_qs(urlparse(url).query)
    assert query["RelayState"] == ["state1"]
    xml = zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15).decode()
    assert f'ID="{rid}"' in xml and f'AssertionConsumerServiceURL="{ACS}"' in xml
    etree.fromstring(xml.encode())
    meta = etree.fromstring(sp_metadata(cfg(), acs_url=ACS).encode())
    ns = {"md": "urn:oasis:names:tc:SAML:2.0:metadata"}
    assert meta.get("entityID") == SP
    assert meta.find("md:SPSSODescriptor/md:AssertionConsumerService", ns).get("Location") == ACS  # type: ignore[union-attr]

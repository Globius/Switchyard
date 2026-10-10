"""SAML 2.0 service-provider side: SP-initiated web SSO only (ADR 0030, decision 10; ADR 0018).

The controller builds an ``AuthnRequest`` (HTTP-Redirect binding), receives the response on the
assertion consumer service (HTTP-POST binding) and turns a *signed* assertion into a plain
:class:`SamlAssertion`. Signature checking uses ``signxml`` against the certificates the
administrator configured (the certificate inside the response is never trusted), and every
security-relevant field is read from the element that the signature actually covers, which defeats
signature-wrapping. Unsigned responses and assertions are always refused.

Example:
    >>> request_id = new_request_id()
    >>> request_id.startswith("_")
    True
"""

import base64
import secrets
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from xml.sax.saxutils import quoteattr

from lxml import etree
from signxml import XMLVerifier  # type: ignore[attr-defined]
from signxml.exceptions import InvalidSignature, SignXMLException

SKEW = timedelta(seconds=60)
NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}
BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
DEFAULT_NAME_ID_FORMAT = "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"
MAX_RESPONSE_BYTES = 512 * 1024


class SamlError(Exception):
    """The SAML response is not acceptable. The message is safe to show and to audit."""


@dataclass(frozen=True)
class SamlConfig:
    """A SAML identity provider as configured by an administrator.

    Attributes:
        id: Provider id.
        name: Provider name.
        idp_entity_id: The IdP's entity id; must equal the assertion ``Issuer``.
        sso_url: The IdP's single sign-on URL (HTTP-Redirect binding).
        signing_certificates: PEM certificates (public) whose key may sign assertions.
        sp_entity_id: Our entity id; the required ``Audience``.
        name_id_format: Requested ``NameIDPolicy`` format.
        attribute_username: Attribute holding the display name (falls back to the NameID).
        attribute_email: Attribute holding the e-mail address.
        attribute_groups: Attribute holding the group names (multi-valued).
    """

    id: str
    name: str
    idp_entity_id: str
    sso_url: str
    signing_certificates: tuple[str, ...]
    sp_entity_id: str
    name_id_format: str = DEFAULT_NAME_ID_FORMAT
    attribute_username: str = "username"
    attribute_email: str = "email"
    attribute_groups: str = "groups"


@dataclass(frozen=True)
class SamlAssertion:
    """What a verified assertion says.

    Attributes:
        assertion_id: Unique id, to be remembered until ``not_on_or_after`` (replay cache).
        not_on_or_after: When the assertion stops being acceptable.
        subject: The ``NameID``.
        attributes: Attribute name to values.
    """

    assertion_id: str
    not_on_or_after: datetime
    subject: str
    attributes: dict[str, list[str]] = field(default_factory=dict)

    def first(self, name: str) -> str | None:
        """First value of an attribute, or ``None``."""
        values = self.attributes.get(name)
        return values[0] if values else None


def new_request_id() -> str:
    """A fresh ``AuthnRequest`` id (an XML ``NCName``, so it starts with a letter or ``_``)."""
    return "_" + secrets.token_hex(20)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def authn_request_url(
    cfg: SamlConfig, *, acs_url: str, request_id: str, relay_state: str, now: datetime
) -> str:
    """The IdP URL to redirect the browser to (HTTP-Redirect binding, unsigned request)."""
    xml = (
        '<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID={quoteattr(request_id)} Version="2.0" IssueInstant={quoteattr(_iso(now))} '
        f"Destination={quoteattr(cfg.sso_url)} "
        'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f"AssertionConsumerServiceURL={quoteattr(acs_url)}>"
        f"<saml:Issuer>{_text(cfg.sp_entity_id)}</saml:Issuer>"
        f'<samlp:NameIDPolicy Format={quoteattr(cfg.name_id_format)} AllowCreate="true"/>'
        "</samlp:AuthnRequest>"
    )
    compressor = zlib.compressobj(wbits=-15)
    deflated = compressor.compress(xml.encode()) + compressor.flush()
    query = urlencode(
        {"SAMLRequest": base64.b64encode(deflated).decode(), "RelayState": relay_state}
    )
    return f"{cfg.sso_url}{'&' if '?' in cfg.sso_url else '?'}{query}"


def _text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sp_metadata(cfg: SamlConfig, *, acs_url: str) -> str:
    """The service-provider metadata an IdP administrator imports.

    Lists the entity id, the assertion consumer service URL and that assertions must be signed.

    Example:
        >>> cfg = SamlConfig("1", "idp", "https://idp", "https://idp/sso", (), "https://sp")
        >>> "AssertionConsumerService" in sp_metadata(cfg, acs_url="https://sp/acs")
        True
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f"entityID={quoteattr(cfg.sp_entity_id)}>"
        '<md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" '
        'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        f"<md:NameIDFormat>{_text(cfg.name_id_format)}</md:NameIDFormat>"
        '<md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location={quoteattr(acs_url)} index="0" isDefault="true"/>'
        "</md:SPSSODescriptor></md:EntityDescriptor>\n"
    )


def _parse_time(value: str | None, what: str) -> datetime:
    if not value:
        raise SamlError(f"the assertion has no {what}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SamlError(f"the assertion's {what} is not a time") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _decode(saml_response: str) -> bytes:
    try:
        raw = base64.b64decode(saml_response, validate=False)
    except ValueError as exc:
        raise SamlError("the SAML response is not valid base64") from exc
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise SamlError("the SAML response is empty or too large")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise SamlError("the SAML response contains a DTD")
    return raw


def _verified_element(cfg: SamlConfig, raw: bytes) -> etree._Element:
    """The element (Response or Assertion) that a configured certificate really signed."""
    if not cfg.signing_certificates:
        raise SamlError("the provider has no signing certificate configured")
    last: Exception | None = None
    for cert in cfg.signing_certificates:
        try:
            result = XMLVerifier().verify(raw, x509_cert=cert)
        except (InvalidSignature, SignXMLException) as exc:
            last = exc
            continue
        results = result if isinstance(result, list) else [result]
        for item in results:
            signed = item.signed_xml
            if etree.QName(signed).namespace == NS["saml"] and etree.QName(signed).localname in (
                "Assertion",
            ):
                return signed
            if etree.QName(signed).localname == "Response":
                return signed
        last = SamlError("the signature does not cover a Response or an Assertion")
    raise SamlError("the SAML response is unsigned or its signature is not valid") from last


def verify_response(
    cfg: SamlConfig, saml_response: str, *, acs_url: str, request_id: str, now: datetime
) -> SamlAssertion:
    """Validate a base64 ``SAMLResponse`` and return its assertion.

    Checks, in this order: size and no DTD; the signature (response or assertion) against the
    configured certificates; the assertion issuer, audience, validity window (60 s skew),
    bearer confirmation recipient, ``InResponseTo`` equal to our request, and ``Destination`` when
    the response itself is signed. Replay detection is the caller's job (``assertion_id``).

    Raises:
        SamlError: For anything that is not acceptable.
    """
    raw = _decode(saml_response)
    signed = _verified_element(cfg, raw)
    if etree.QName(signed).localname == "Response":
        if signed.get("Destination") not in (None, acs_url):
            raise SamlError("the response was sent to another service provider")
        status = signed.find("samlp:Status/samlp:StatusCode", NS)
        if status is None or status.get("Value") != STATUS_SUCCESS:
            raise SamlError("the identity provider reported a failure")
        assertions = signed.findall("saml:Assertion", NS)
        if len(assertions) != 1:
            raise SamlError("the response must carry exactly one assertion")
        assertion = assertions[0]
    else:
        assertion = signed
    issuer = assertion.findtext("saml:Issuer", namespaces=NS)
    if (issuer or "").strip() != cfg.idp_entity_id:
        raise SamlError("the assertion was issued by another identity provider")
    conditions = assertion.find("saml:Conditions", NS)
    if conditions is None:
        raise SamlError("the assertion has no conditions")
    not_before = conditions.get("NotBefore")
    if not_before and _parse_time(not_before, "NotBefore") - SKEW > now:
        raise SamlError("the assertion is not valid yet")
    not_on_or_after = _parse_time(conditions.get("NotOnOrAfter"), "NotOnOrAfter")
    if not_on_or_after + SKEW <= now:
        raise SamlError("the assertion has expired")
    audiences = [
        (a.text or "").strip()
        for a in conditions.findall("saml:AudienceRestriction/saml:Audience", NS)
    ]
    if cfg.sp_entity_id not in audiences:
        raise SamlError("the assertion is meant for another audience")
    _check_confirmation(assertion, acs_url=acs_url, request_id=request_id, now=now)
    name_id = assertion.findtext("saml:Subject/saml:NameID", namespaces=NS)
    assertion_id = assertion.get("ID")
    if not name_id or not name_id.strip() or not assertion_id:
        raise SamlError("the assertion has no subject or id")
    attributes: dict[str, list[str]] = {}
    for attribute in assertion.findall("saml:AttributeStatement/saml:Attribute", NS):
        values = [
            (v.text or "").strip() for v in attribute.findall("saml:AttributeValue", NS) if v.text
        ]
        for key in (attribute.get("Name"), attribute.get("FriendlyName")):
            if key:
                attributes.setdefault(key, []).extend(values)
    return SamlAssertion(assertion_id, not_on_or_after + SKEW, name_id.strip(), attributes)


def _check_confirmation(
    assertion: etree._Element, *, acs_url: str, request_id: str, now: datetime
) -> None:
    for confirmation in assertion.findall("saml:Subject/saml:SubjectConfirmation", NS):
        data = confirmation.find("saml:SubjectConfirmationData", NS)
        if confirmation.get("Method") != BEARER or data is None:
            continue
        if data.get("InResponseTo") != request_id:
            raise SamlError("the response answers a different login request")
        if data.get("Recipient") != acs_url:
            raise SamlError("the assertion is addressed to another service provider")
        if _parse_time(data.get("NotOnOrAfter"), "confirmation time") + SKEW <= now:
            raise SamlError("the assertion has expired")
        return
    raise SamlError("the assertion has no usable bearer confirmation")

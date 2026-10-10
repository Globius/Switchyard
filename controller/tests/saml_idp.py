"""A tiny SAML identity provider for tests: makes a key and certificate and signs responses."""

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner, methods

IDP = "https://idp.example.test/entity"
SP = "https://switchyard.example.test/api/v1/auth/saml/idp"
ACS = "https://switchyard.example.test/api/v1/auth/callback/idp"
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class IdpKeys:
    """Signing key and certificate of the fake IdP."""

    key_pem: bytes
    cert_pem: str


def make_idp(common_name: str = "idp.example.test") -> IdpKeys:
    """Generate a fresh RSA key and self-signed certificate."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    return IdpKeys(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        cert.public_bytes(serialization.Encoding.PEM).decode(),
    )


def response_xml(
    request_id: str,
    *,
    assertion_id: str = "_assertion1",
    issuer: str = IDP,
    audience: str = SP,
    recipient: str = ACS,
    destination: str = ACS,
    in_response_to: str | None = None,
    not_before: datetime | None = None,
    not_on_or_after: datetime | None = None,
    subject: str = "alice-id",
    groups: tuple[str, ...] = ("sy-admins",),
    status: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
    now: datetime = NOW,
) -> str:
    """An unsigned ``Response`` carrying one assertion; override fields to make it wrong."""
    nb = (not_before or now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    na = (not_on_or_after or now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    values = "".join(f"<saml:AttributeValue>{g}</saml:AttributeValue>" for g in groups)
    return (
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_resp1" Version="2.0" '
        f'IssueInstant="{stamp}" Destination="{destination}" '
        f'InResponseTo="{in_response_to or request_id}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f'<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>'
        f'<saml:Assertion ID="{assertion_id}" Version="2.0" IssueInstant="{stamp}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f"<saml:Subject><saml:NameID>{subject}</saml:NameID>"
        '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData InResponseTo="{in_response_to or request_id}" '
        f'Recipient="{recipient}" NotOnOrAfter="{na}"/></saml:SubjectConfirmation></saml:Subject>'
        f'<saml:Conditions NotBefore="{nb}" NotOnOrAfter="{na}">'
        f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience>"
        "</saml:AudienceRestriction></saml:Conditions>"
        "<saml:AttributeStatement>"
        '<saml:Attribute Name="username"><saml:AttributeValue>alice</saml:AttributeValue>'
        "</saml:Attribute>"
        '<saml:Attribute Name="email"><saml:AttributeValue>alice@example.test</saml:AttributeValue>'
        "</saml:Attribute>"
        f'<saml:Attribute Name="groups">{values}</saml:Attribute>'
        "</saml:AttributeStatement></saml:Assertion></samlp:Response>"
    )


def sign(xml: str, idp: IdpKeys, *, what: str = "assertion") -> str:
    """Sign the assertion (default) or the whole response."""
    root = etree.fromstring(xml.encode())
    target = root
    if what == "assertion":
        target = root.find("{urn:oasis:names:tc:SAML:2.0:assertion}Assertion")
    signer = XMLSigner(
        method=methods.enveloped, c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"
    )
    signed = signer.sign(
        target, key=idp.key_pem, cert=idp.cert_pem, reference_uri=f"#{target.get('ID')}"
    )
    if what == "assertion":
        root.replace(target, signed)
        signed = root
    return etree.tostring(signed).decode()


def encode(xml: str) -> str:
    """Base64 the way a browser form posts ``SAMLResponse``."""
    return base64.b64encode(xml.encode()).decode()

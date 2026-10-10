"""Certificates: parsing and checking PEM material, generating keys and CSRs, importing.

Private keys are generated in the worker, written to the secrets backend and never returned,
logged or stored anywhere else. The ``certificate`` object only carries the reference to them.
Every stored secret gets a *new* reference (it ends with the leaf serial number), so renewing a
certificate changes the object and the apply pipeline sees a real difference to roll out.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from switchyard.domain.validators import validate_host_pattern


class CertificateError(ValueError):
    """The supplied certificate material is unusable."""


@dataclass(frozen=True)
class CertInfo:
    """What the controller records about a certificate.

    Attributes:
        sans: DNS names (lower-case, wildcards cover one label), sorted.
        not_before: Start of validity (UTC).
        not_after: End of validity (UTC).
        serial: Leaf serial number in hex, used in the secret reference.
        pem: Normalized PEM: the certificate chain as given, then the PKCS#8 private key.
    """

    sans: tuple[str, ...]
    not_before: datetime
    not_after: datetime
    serial: str
    pem: str


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def inspect_pem(pem: str, *, now: datetime | None = None) -> CertInfo:
    """Check a PEM bundle (chain plus unencrypted private key) and describe it.

    The first certificate is the leaf. Its public key must match the private key, it must be
    valid now, and it must name at least one DNS name that passes the hostname rules.

    Raises:
        CertificateError: On any problem, with a message safe to show to the user.
    """
    now = now or datetime.now(UTC)
    blocks = pem.split("-----BEGIN ")[1:]
    certs: list[x509.Certificate] = []
    keys: list[bytes] = []
    for block in blocks:
        text = "-----BEGIN " + block
        if block.startswith("CERTIFICATE-----"):
            try:
                certs.append(x509.load_pem_x509_certificate(text.encode()))
            except ValueError as exc:
                raise CertificateError("a certificate in the bundle is malformed") from exc
        elif "PRIVATE KEY-----" in block.split("\n", 1)[0]:
            keys.append(text.encode())
        else:
            raise CertificateError("only certificates and one private key are allowed")
    if not certs:
        raise CertificateError("the bundle holds no certificate")
    if len(keys) != 1:
        raise CertificateError("the bundle must hold exactly one private key")
    try:
        key = serialization.load_pem_private_key(keys[0], password=None)
    except (ValueError, TypeError) as exc:
        raise CertificateError("the private key is malformed or encrypted") from exc
    leaf = certs[0]
    spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if key.public_key().public_bytes(*spki) != leaf.public_key().public_bytes(*spki):
        raise CertificateError("the private key does not match the first certificate")
    not_before, not_after = _utc(leaf.not_valid_before_utc), _utc(leaf.not_valid_after_utc)
    if not_after <= now:
        raise CertificateError(f"the certificate expired on {not_after:%Y-%m-%d}")
    if not_before > now:
        raise CertificateError("the certificate is not valid yet")
    names: list[str] = []
    try:
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = list(san.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        pass
    if not names:
        names = [str(a.value) for a in leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    try:
        sans = tuple(sorted({validate_host_pattern(str(n).lower()) for n in names}))
    except ValueError as exc:
        raise CertificateError(f"unsupported name in the certificate: {exc}") from exc
    if not sans:
        raise CertificateError("the certificate names no DNS host")
    normalized = (
        "".join(c.public_bytes(serialization.Encoding.PEM).decode() for c in certs)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
    )
    return CertInfo(sans, not_before, not_after, format(leaf.serial_number, "x"), normalized)


def generate_key_and_csr(sans: tuple[str, ...]) -> tuple[str, str]:
    """Create an ECDSA P-256 key and a CSR for ``sans``. Returns ``(key_pem, csr_pem)``.

    Example:
        >>> key, csr = generate_key_and_csr(("example.test", "*.example.test"))
        >>> key.startswith("-----BEGIN PRIVATE KEY-----")
        True
        >>> csr.startswith("-----BEGIN CERTIFICATE REQUEST")
        True
    """
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sans[0][:64])]))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key_pem, csr.public_bytes(serialization.Encoding.PEM).decode()


def secret_ref_for(tenant_name: str, cert_name: str, serial: str) -> str:
    """Return the versioned OpenBao reference for a certificate's key material.

    Example:
        >>> secret_ref_for("acme", "wild-example-test", "ab12")
        'kv/tenants/acme/certs/wild-example-test/ab12'
    """
    return f"kv/tenants/{tenant_name}/certs/{cert_name}/{serial}"

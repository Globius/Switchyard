"""Certificates: parsing and checking PEM material, generating keys and CSRs, importing.

Private keys are generated in the worker, written to the secrets backend and never returned,
logged or stored anywhere else. The ``certificate`` object only carries the reference to them.
Every stored secret gets a *new* reference (it ends with the leaf serial number), so renewing a
certificate changes the object and the apply pipeline sees a real difference to roll out.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
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


class Pkcs12Error(CertificateError):
    """A PKCS#12 upload is unusable; ``path`` names the request field at fault."""

    def __init__(self, path: str, code: str, message: str) -> None:
        """Create the error for the request field ``path``."""
        super().__init__(message)
        self.path = path
        self.code = code


def pkcs12_to_pem(pkcs12_b64: str, password: str | None) -> str:
    """Unpack a base64 PKCS#12/PFX into the PEM bundle ``inspect_pem`` expects.

    The result is the leaf, then the chain, then the unencrypted PKCS#8 key. The key must match
    the leaf (``inspect_pem`` checks that afterwards). The password is only used to decrypt and is
    never kept. Legacy files (RC2/3DES-era encryption that OpenSSL 3 no longer reads) are refused
    with advice to re-export them.

    Example:
        >>> pkcs12_to_pem("!!", None)
        Traceback (most recent call last):
        ...
        switchyard.services.certs.Pkcs12Error: pkcs12_b64 is not valid base64

    Raises:
        Pkcs12Error: On bad base64, a wrong password, unsupported encryption or missing key.
    """
    try:
        raw = base64.b64decode(pkcs12_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Pkcs12Error("pkcs12_b64", "invalid", "pkcs12_b64 is not valid base64") from exc
    try:
        key, leaf, chain = pkcs12.load_key_and_certificates(
            raw, password.encode() if password else None
        )
    except ValueError as exc:
        text = str(exc).lower()
        if "mac" in text or "password" in text:
            raise Pkcs12Error(
                "password", "wrong_password", "wrong password for the PKCS#12 file"
            ) from exc
        if "unsupported" in text or "rc2" in text or "legacy" in text:
            raise Pkcs12Error(
                "pkcs12_b64",
                "unsupported_encryption",
                "this PKCS#12 uses legacy encryption (RC2/3DES) that is not supported; "
                "re-export it with AES, for example: openssl pkcs12 -export -aes256 ...",
            ) from exc
        raise Pkcs12Error(
            "pkcs12_b64", "invalid", "pkcs12_b64 is not a readable PKCS#12 file"
        ) from exc
    except TypeError as exc:
        raise Pkcs12Error(
            "password", "wrong_password", "the PKCS#12 file needs a password, or has none"
        ) from exc
    if key is None:
        raise Pkcs12Error("pkcs12_b64", "no_key", "the PKCS#12 file holds no private key")
    if leaf is None:
        raise Pkcs12Error("pkcs12_b64", "no_certificate", "the PKCS#12 file holds no certificate")
    enc = serialization.Encoding.PEM
    return (
        "".join(c.public_bytes(enc).decode() for c in [leaf, *chain])
        + key.private_bytes(
            enc, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
    )


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

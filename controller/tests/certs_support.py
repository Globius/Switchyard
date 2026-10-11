"""Throw-away certificates for tests (self-signed, ECDSA, generated on the fly)."""

from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def make_pem(
    sans: tuple[str, ...] = ("example.test", "*.example.test"),
    *,
    days: int = 90,
    started_days_ago: int = 1,
    with_key: bool = True,
    other_key: bool = False,
    key_pem: str | None = None,
) -> str:
    """Return ``certificate + key`` PEM text for a self-signed certificate.

    Args:
        sans: DNS names in the certificate.
        days: Days until expiry, counted from now (negative for an expired certificate).
        started_days_ago: How long ago it became valid.
        with_key: Include the private key in the output.
        other_key: Output a key that does not belong to the certificate.
        key_pem: Sign with this PKCS#8 key instead of a new one.
    """
    key = (
        serialization.load_pem_private_key(key_pem.encode(), password=None)
        if key_pem
        else ec.generate_private_key(ec.SECP256R1())
    )
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sans[0][:64])])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=started_days_ago))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False)
        .sign(key, hashes.SHA256())
    )
    out = cert.public_bytes(serialization.Encoding.PEM).decode()
    if with_key:
        signing = ec.generate_private_key(ec.SECP256R1()) if other_key else key
        out += signing.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
    return out


def sign_csr(csr_pem: str, *, days: int = 365 * 15) -> str:
    """Sign a CSR with a throw-away CA key, like Cloudflare's Origin CA would. Returns the PEM."""
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Origin CA")])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_name)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=days))
    )
    for ext in csr.extensions:
        builder = builder.add_extension(ext.value, ext.critical)
    return builder.sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()


def make_ca_pem(*, ca: bool = True, days: int = 3650) -> tuple[str, str]:
    """Return ``(certificate PEM, empty CRL PEM)`` of a throw-away CA (no key in the output)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test client ca")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=10))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(name)
        .last_update(now)
        .next_update(now + timedelta(days=7))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return pem, crl.public_bytes(serialization.Encoding.PEM).decode()

"""The TLS-ALPN-01 challenge certificate (RFC 8737)."""

import hashlib

from cryptography import x509

from switchyard.adapters.acme import ACME_IDENTIFIER_OID, challenge_certificate


def test_the_certificate_names_the_domain_and_carries_the_key_authorization_digest() -> None:
    pem = challenge_certificate("a.example.test", "token.thumbprint")
    cert = x509.load_pem_x509_certificate(pem.encode())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["a.example.test"]
    ext = cert.extensions.get_extension_for_oid(x509.ObjectIdentifier(ACME_IDENTIFIER_OID))
    assert ext.critical
    assert ext.value.value == b"\x04\x20" + hashlib.sha256(b"token.thumbprint").digest()
    assert "PRIVATE KEY" in pem, "the key travels with it so the engine can serve it"
    again = challenge_certificate("a.example.test", "token.thumbprint")
    assert again != pem, "a fresh key each time"

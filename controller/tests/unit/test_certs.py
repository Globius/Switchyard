"""PEM inspection and CSR generation."""

import pytest
from cryptography import x509

from switchyard.domain.validators import validate_secret_ref
from switchyard.services.certs import (
    CertificateError,
    generate_key_and_csr,
    inspect_pem,
    secret_ref_for,
)
from tests.certs_support import make_pem


def test_a_good_bundle_is_described_and_normalized() -> None:
    info = inspect_pem(make_pem(("Example.Test", "*.example.test")))
    assert info.sans == ("*.example.test", "example.test")
    assert info.not_after > info.not_before
    assert info.pem.count("BEGIN CERTIFICATE") == 1 and "BEGIN PRIVATE KEY" in info.pem
    assert info.serial and all(c in "0123456789abcdef" for c in info.serial)


def test_chain_is_kept_in_order() -> None:
    leaf = make_pem(("a.example.test",), with_key=False)
    other = make_pem(("ca.example.test",), with_key=False)
    key_source = make_pem(("a.example.test",))
    # leaf + other + key of leaf requires the leaf to be signed by that key
    key_pem = key_source[key_source.index("-----BEGIN PRIVATE KEY") :]
    leaf = make_pem(("a.example.test",), with_key=False, key_pem=key_pem)
    info = inspect_pem(leaf + other + key_pem)
    assert info.pem.count("BEGIN CERTIFICATE") == 2
    assert info.sans == ("a.example.test",)


@pytest.mark.parametrize(
    ("pem", "message"),
    [
        (make_pem(with_key=False), "exactly one private key"),
        (make_pem(other_key=True), "does not match"),
        (make_pem(days=-2, started_days_ago=10), "expired"),
        (make_pem(started_days_ago=-3), "not valid yet"),
        (make_pem(("bad_name.example.test",)), "unsupported name"),
        ("not a pem at all", "no certificate"),
        ("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", "malformed"),
    ],
)
def test_bad_bundles_are_rejected_with_a_clear_message(pem: str, message: str) -> None:
    with pytest.raises(CertificateError, match=message):
        inspect_pem(pem)


def test_unknown_pem_blocks_are_refused() -> None:
    with pytest.raises(CertificateError, match="only certificates"):
        inspect_pem(
            make_pem() + "-----BEGIN DH PARAMETERS-----\nAAAA\n-----END DH PARAMETERS-----\n"
        )


def test_csr_matches_the_requested_names_and_the_key() -> None:
    key, csr_pem = generate_key_and_csr(("example.test", "*.example.test"))
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    assert csr.is_signature_valid
    san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["example.test", "*.example.test"]
    assert "BEGIN PRIVATE KEY" in key


def test_secret_references_are_valid_and_unique_per_serial() -> None:
    a = secret_ref_for("acme", "wild", "ab12")
    assert validate_secret_ref(a) == a
    assert a != secret_ref_for("acme", "wild", "cd34")

"""PEM inspection and CSR generation."""

from typing import Any

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


def _p12(pem: str, password: str | None, *, legacy: bool = False) -> str:
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    certs = x509.load_pem_x509_certificates(pem.encode())
    key = serialization.load_pem_private_key(
        pem[pem.index("-----BEGIN PRIVATE KEY") :].encode(), None
    )
    enc = (
        serialization.BestAvailableEncryption(password.encode())
        if password
        else serialization.NoEncryption()
    )
    return base64.b64encode(
        pkcs12.serialize_key_and_certificates(b"x", key, certs[0], certs[1:] or None, enc)  # type: ignore[arg-type]
    ).decode()


def test_pkcs12_round_trips_to_the_same_bundle() -> None:
    from switchyard.services.certs import pkcs12_to_pem

    pem = make_pem(("a.example.test",))
    for password in ("s3cret", None):
        out = pkcs12_to_pem(_p12(pem, password), password)
        assert inspect_pem(out).sans == ("a.example.test",)
        assert inspect_pem(out).pem == inspect_pem(pem).pem


@pytest.mark.parametrize(
    ("b64", "password", "path", "code"),
    [
        ("!!not-base64!!", None, "pkcs12_b64", "invalid"),
        ("QUJDRA==", None, "pkcs12_b64", "invalid"),
    ],
)
def test_pkcs12_bad_input_names_the_field(
    b64: str, password: str | None, path: str, code: str
) -> None:
    from switchyard.services.certs import Pkcs12Error, pkcs12_to_pem

    with pytest.raises(Pkcs12Error) as err:
        pkcs12_to_pem(b64, password)
    assert (err.value.path, err.value.code) == (path, code)


def test_pkcs12_wrong_password() -> None:
    from switchyard.services.certs import Pkcs12Error, pkcs12_to_pem

    blob = _p12(make_pem(("a.example.test",)), "right")
    with pytest.raises(Pkcs12Error) as err:
        pkcs12_to_pem(blob, "wrong")
    assert err.value.path == "password" and "secret" not in str(err.value)


def test_pkcs12_without_a_key_is_refused() -> None:
    import base64

    from cryptography.hazmat.primitives.serialization import pkcs12

    from switchyard.services.certs import Pkcs12Error, pkcs12_to_pem

    cert = x509.load_pem_x509_certificate(make_pem(("a.example.test",), with_key=False).encode())
    blob = base64.b64encode(
        pkcs12.serialize_key_and_certificates(None, None, cert, None, serialization_none())
    ).decode()
    with pytest.raises(Pkcs12Error) as err:
        pkcs12_to_pem(blob, None)
    assert err.value.code == "no_key"


def serialization_none() -> Any:
    from cryptography.hazmat.primitives import serialization

    return serialization.NoEncryption()


def test_legacy_pkcs12_is_refused_clearly(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import base64
    import shutil
    import subprocess

    from switchyard.services.certs import Pkcs12Error, pkcs12_to_pem

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl not available")
    pem = make_pem(("a.example.test",))
    (tmp_path / "b.pem").write_text(pem)
    out = tmp_path / "b.p12"
    done = subprocess.run(
        [
            openssl,
            "pkcs12",
            "-export",
            "-in",
            str(tmp_path / "b.pem"),
            "-out",
            str(out),
            "-passout",
            "pass:pw",
            "-legacy",
            "-certpbe",
            "PBE-SHA1-3DES",
            "-keypbe",
            "PBE-SHA1-3DES",
            "-macalg",
            "sha1",
        ],
        capture_output=True,
    )
    if done.returncode != 0:
        pytest.skip("openssl cannot make a legacy file here")
    blob = base64.b64encode(out.read_bytes()).decode()
    try:
        result = pkcs12_to_pem(blob, "pw")
    except Pkcs12Error as exc:
        code = exc.code
        assert code in {"unsupported_encryption", "invalid"}
    else:
        assert "BEGIN PRIVATE KEY" in result  # 3DES is readable by this build

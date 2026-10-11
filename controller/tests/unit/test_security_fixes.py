"""Fixes from the backend security review (docs/security/backend-review.md)."""

import copy

import pytest

from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import validate_snapshot
from switchyard.domain.validators import check_secret_ref_scope
from switchyard.services.sessions import safe_return_to
from tests.factories import BASE


@pytest.mark.parametrize(
    "ref",
    [
        "kv/tenants/acme/certs/x/01",
        "kv/lab",
        "kv/se/dpapi",
        "kv/bgp/peer-1",
    ],
)
def test_own_and_neutral_secret_references_are_allowed(ref: str) -> None:
    assert check_secret_ref_scope(ref, "acme") is None


@pytest.mark.parametrize(
    "ref",
    ["kv/tenants/globex/certs/x/01", "kv/tenants/acmeevil/x", "kv/tenants/", "kv/proxmox/token"],
)
def test_other_tenants_and_platform_secrets_are_refused(ref: str) -> None:
    assert check_secret_ref_scope(ref, "acme")


def test_a_certificate_cannot_point_at_another_tenants_key() -> None:
    d = copy.deepcopy(BASE)
    d["certificates"][0]["secret_ref"] = "kv/tenants/globex/certs/wild/01"
    problems = [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(d))]
    assert problems == [("certificates.wild-example-test.secret_ref", "secret_ref_not_allowed")]


def test_a_bgp_password_cannot_point_at_a_platform_secret() -> None:
    d = copy.deepcopy(BASE)
    d["routing_profiles"] = [
        {
            "id": "rp",
            "tenant_id": "t-acme",
            "name": "bgp",
            "kind": "bgp",
            "asn": 65000,
            "peers": [{"peer_ip": "192.0.2.1", "remote_asn": 65001, "md5_ref": "kv/proxmox/token"}],
        }
    ]
    codes = [p.code for p in validate_snapshot(snapshot_from_mapping(d))]
    assert "secret_ref_not_allowed" in codes


@pytest.mark.parametrize("value", ["/\t/evil.example", "/\n/evil.example", "/a\rb", "/\x00"])
def test_return_to_refuses_control_characters(value: str) -> None:
    assert safe_return_to(value) == "/"


def test_return_to_keeps_normal_paths() -> None:
    assert safe_return_to("/t/acme/pools?x=1") == "/t/acme/pools?x=1"


def test_csv_cells_cannot_start_a_formula() -> None:
    from switchyard.api.app import _csv_safe

    for evil in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx"):
        assert _csv_safe(evil).startswith("'")
    assert _csv_safe("user:alice") == "user:alice"


def test_an_agent_without_a_ca_is_refused_unless_explicitly_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from switchyard import runtime

    key = tmp_path / "bundle.pem"  # type: ignore[operator]
    key.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setenv("SWITCHYARD_BUNDLE_KEY_FILE", str(Path(str(key))))
    monkeypatch.delenv("SWITCHYARD_DATAPLANE_CA", raising=False)
    monkeypatch.delenv("SWITCHYARD_INSECURE_AGENT", raising=False)
    monkeypatch.setenv("SWITCHYARD_DATABASE_URL", "postgresql+psycopg://x@localhost/x")
    with pytest.raises(RuntimeError, match="without TLS"):
        runtime.directory(None)  # type: ignore[arg-type]
    monkeypatch.setenv("SWITCHYARD_INSECURE_AGENT", "1")
    assert runtime.directory(None) is not None  # type: ignore[arg-type]

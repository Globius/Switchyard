# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Client CAs for mutual TLS (ADR 0070): the bundle check and the model rules."""

import copy

import pytest
from pydantic import ValidationError

from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.models import VsPort
from switchyard.domain.snapshot import validate_snapshot
from switchyard.services.certs import CertificateError, inspect_ca_pem
from tests.certs_support import make_ca_pem, make_pem
from tests.factories import BASE


def test_a_ca_bundle_is_accepted_with_or_without_a_crl() -> None:
    pem, crl = make_ca_pem()
    info = inspect_ca_pem(pem)
    assert info.crl_pem is None and "BEGIN CERTIFICATE" in info.pem and info.serial
    assert inspect_ca_pem(pem, crl).crl_pem and "X509 CRL" in (
        inspect_ca_pem(pem, crl).crl_pem or ""
    )


@pytest.mark.parametrize(
    ("pem", "message"),
    [
        (make_pem(), "private key"),
        (make_ca_pem(ca=False)[0], "must be a CA"),
        (make_ca_pem(days=-2)[0], "expired"),
        ("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", "malformed"),
        ("", "no certificate"),
    ],
)
def test_bad_bundles_are_refused(pem: str, message: str) -> None:
    with pytest.raises(CertificateError, match=message):
        inspect_ca_pem(pem)


def test_a_bad_crl_is_refused() -> None:
    with pytest.raises(CertificateError, match="not a PEM CRL"):
        inspect_ca_pem(make_ca_pem()[0], "nope")


def test_ports_need_tls_for_a_client_ca() -> None:
    import uuid

    assert VsPort(port=443, tls=True, client_ca_id=uuid.uuid4()).client_verify == "required"
    with pytest.raises(ValidationError, match="client_ca_id requires tls"):
        VsPort(port=80, client_ca_id=uuid.uuid4())


def data(**ca: object) -> dict:  # type: ignore[type-arg]
    d = copy.deepcopy(BASE)
    d["certificates"].append(
        {
            "id": "ca-1",
            "tenant_id": "t-acme",
            "name": "partners",
            "source": "upload",
            "purpose": "client_ca",
            "sans": [],
            "not_after": "2038-01-01T00:00:00+00:00",
            "secret_ref": "kv/x",
            **ca,
        }
    )
    d["virtual_services"][0]["ports"][0]["client_ca_id"] = "ca-1"
    return d


def problems(d: dict) -> list[tuple[str, str]]:  # type: ignore[type-arg]
    return [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(d))]


def test_a_port_may_reference_a_client_ca_and_nothing_else_may() -> None:
    assert problems(data()) == []
    d = data()
    d["virtual_services"][0]["certificate_ids"].append("ca-1")
    assert ("virtual_services.web.certificate_ids[1]", "wrong_certificate_purpose") in problems(d)
    d = data()
    d["virtual_services"][0]["ports"][0]["client_ca_id"] = "cert-wild"
    assert ("virtual_services.web.ports[0].client_ca_id", "wrong_certificate_purpose") in problems(
        d
    )


def test_a_client_ca_has_no_names_or_renewal_and_a_crl_needs_a_ca() -> None:
    for bad in ({"sans": ["x.test"]}, {"renew_policy": "auto"}, {"ocsp_stapling": True}):
        with pytest.raises(ValidationError):
            snapshot_from_mapping(data(**bad))
    d = copy.deepcopy(BASE)
    d["certificates"][0]["crl_secret_ref"] = "kv/crl"
    with pytest.raises(ValidationError, match="only valid on a client_ca"):
        snapshot_from_mapping(d)
    d = copy.deepcopy(BASE)
    d["certificates"][0]["sans"] = []
    with pytest.raises(ValidationError, match="at least one name"):
        snapshot_from_mapping(d)


async def test_the_bundle_carries_the_ca_and_crl_but_never_a_key() -> None:
    from switchyard.adapters.secrets import MemorySecrets
    from switchyard.pipeline.bundles import BundleError, engine_files
    from switchyard.render.routing import EngineSlot

    pem, crl = make_ca_pem()
    d = data(crl_secret_ref="kv/crl")
    d["virtual_services"][0]["ports"][0]["tls"] = True
    snap = snapshot_from_mapping(d)
    group = snap.se_groups[0]
    slot = EngineSlot("se-a", 0, "10.0.0.1")
    server = make_pem()
    secrets = MemorySecrets(
        {"kv/x": pem, "kv/crl": crl, "kv/tenants/acme/certs/wild-example-test": server}
    )
    files = await engine_files(snap, group, slot, secrets)
    assert files["certs/ca-partners.pem"].startswith("-----BEGIN CERTIFICATE")
    assert files["certs/ca-partners.crl"].startswith("-----BEGIN X509 CRL")
    assert "PRIVATE KEY" not in files["certs/ca-partners.pem"]
    assert "certs/wild-example-test.pem" in files
    bad = MemorySecrets({"kv/x": server, "kv/tenants/acme/certs/wild-example-test": server})
    with pytest.raises(BundleError, match="certificates only"):
        await engine_files(snap, group, slot, bad)

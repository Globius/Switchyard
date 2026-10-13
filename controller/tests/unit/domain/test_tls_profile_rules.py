# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""TLS profiles on ports and OCSP stapling on certificates (ADR 0067)."""

import datetime

import pytest
from pydantic import ValidationError

from switchyard.domain.models import Certificate, TlsProfile, VsPort


def test_a_profile_needs_a_tls_port() -> None:
    assert (
        VsPort(port=443, tls=True, tls_profile=TlsProfile.MODERN).tls_profile is TlsProfile.MODERN
    )
    assert VsPort(port=443, tls=True).tls_profile is None, "no profile keeps the engine default"
    with pytest.raises(ValidationError, match="tls_profile requires tls"):
        VsPort(port=80, tls_profile=TlsProfile.MODERN)
    with pytest.raises(ValidationError):
        VsPort(port=443, tls=True, tls_profile="sslv3")


def cert(**kw: object) -> Certificate:
    import uuid

    base = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "name": "c",
        "source": "acme",
        "sans": ["example.test"],
        "not_after": datetime.datetime(2038, 1, 1, tzinfo=datetime.UTC),
        "secret_ref": "kv/x",
    }
    return Certificate(**(base | kw))


def test_ocsp_stapling_is_off_by_default_and_refused_for_origin_ca_certificates() -> None:
    assert cert().ocsp_stapling is False
    assert cert(ocsp_stapling=True).ocsp_stapling is True
    with pytest.raises(ValidationError, match="no OCSP responder"):
        cert(source="cloudflare_origin", ocsp_stapling=True)

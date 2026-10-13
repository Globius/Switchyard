# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Internal PKI: signs the agents' server certificates.

The controller never sees an agent's private key. The agent generates its key, sends a CSR, and
gets back a short-lived certificate whose names are set here from the engine record, not taken from
the CSR. :class:`OpenBaoPki` signs through OpenBao's PKI engine; :class:`LocalCa` is an
in-process CA for development and tests.
"""

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from switchyard.adapters.secrets import OpenBaoSecrets, SecretBackendError


class PkiError(Exception):
    """A certificate could not be signed."""


@dataclass(frozen=True)
class SignedCert:
    """A signed certificate and the CA that issued it (both PEM), plus its serial in hex."""

    cert_pem: str
    ca_pem: str
    serial: str


class PkiSigner(Protocol):
    """Signs a CSR for one engine."""

    async def sign(
        self,
        csr_pem: str,
        *,
        common_name: str,
        ip_sans: tuple[str, ...],
        ttl: timedelta,
        usage: Literal["server", "client"] = "server",
        uri_sans: tuple[str, ...] = (),
    ) -> SignedCert:
        """Return a certificate for the CSR's public key with exactly the given names.

        ``usage`` ``client`` makes a client-authentication certificate (a device, ADR 0196); its
        only name is ``common_name`` plus the ``uri_sans``.
        """
        ...


def load_csr(csr_pem: str) -> x509.CertificateSigningRequest:
    """Parse a CSR and check its signature and key strength.

    Raises:
        PkiError: If it is malformed, badly signed or the key is too weak.
    """
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
    except ValueError as exc:
        raise PkiError("the certificate request is malformed") from exc
    if not csr.is_signature_valid:
        raise PkiError("the certificate request signature is invalid")
    key = csr.public_key()
    if isinstance(key, ec.EllipticCurvePublicKey):
        ok = key.curve.name in {"secp256r1", "secp384r1"}
    else:
        ok = getattr(key, "key_size", 0) >= 2048
    if not ok:
        raise PkiError("the request uses a weak or unsupported key")
    return csr


class LocalCa:
    """A CA held in memory (development and tests). Not for production use.

    Example:
        >>> ca = LocalCa()
        >>> ca.ca_pem.startswith("-----BEGIN CERTIFICATE-----")
        True
    """

    def __init__(self) -> None:
        """Create a fresh ECDSA CA valid for ten years."""
        self._key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Switchyard development CA")])
        now = datetime.now(UTC)
        self._cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(self._key, hashes.SHA256())
        )

    @property
    def ca_pem(self) -> str:
        """The CA certificate."""
        return self._cert.public_bytes(serialization.Encoding.PEM).decode()

    async def sign(
        self,
        csr_pem: str,
        *,
        common_name: str,
        ip_sans: tuple[str, ...],
        ttl: timedelta,
        usage: Literal["server", "client"] = "server",
        uri_sans: tuple[str, ...] = (),
    ) -> SignedCert:
        """Sign for the CSR's public key with the given names (server or client authentication)."""
        csr = load_csr(csr_pem)
        now = datetime.now(UTC)
        names: list[x509.GeneralName] = (
            [x509.DNSName(common_name)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ip_sans]
            if usage == "server"
            else []
        )
        names += [x509.UniformResourceIdentifier(u) for u in uri_sans]
        usages = [
            ExtendedKeyUsageOID.SERVER_AUTH
            if usage == "server"
            else ExtendedKeyUsageOID.CLIENT_AUTH
        ]
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(self._cert.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + ttl)
            .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
        )
        if names:
            builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
        cert = builder.sign(self._key, hashes.SHA256())
        return SignedCert(
            cert.public_bytes(serialization.Encoding.PEM).decode(),
            self.ca_pem,
            format(cert.serial_number, "x"),
        )


class OpenBaoPki:
    """Signs through an OpenBao PKI secrets engine role (``<mount>/sign/<role>``)."""

    def __init__(self, bao: OpenBaoSecrets, *, mount: str = "pki", role: str = "agents") -> None:
        """Use ``bao`` (already authenticated by AppRole) and the given mount and role."""
        self._bao = bao
        self._path = f"{mount}/sign/{role}"

    async def sign(
        self,
        csr_pem: str,
        *,
        common_name: str,
        ip_sans: tuple[str, ...],
        ttl: timedelta,
        usage: Literal["server", "client"] = "server",
        uri_sans: tuple[str, ...] = (),
    ) -> SignedCert:
        """Ask OpenBao to sign the CSR (a client certificate needs a role with ``client_flag``)."""
        load_csr(csr_pem)
        request: dict[str, object] = {
            "csr": csr_pem,
            "common_name": common_name,
            "ttl": f"{int(ttl.total_seconds())}s",
            "format": "pem",
        }
        if usage == "server":
            request |= {"alt_names": common_name, "ip_sans": ",".join(ip_sans)}
        if uri_sans:
            request["uri_sans"] = ",".join(uri_sans)
        try:
            data = await self._bao.call(self._path, request)
        except SecretBackendError as exc:
            raise PkiError(str(exc)) from exc
        cert, ca = data.get("certificate"), data.get("issuing_ca")
        if not isinstance(cert, str) or not isinstance(ca, str):
            raise PkiError("OpenBao returned no certificate")
        serial = str(data.get("serial_number", "")).replace(":", "").lower()
        return SignedCert(cert, ca, serial)

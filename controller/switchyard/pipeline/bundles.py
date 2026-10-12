"""The signed file set one engine's agent should hold: routing config plus TLS certificates.

The apply pipeline sends it and the status poller compares against it, so both build it here.
Private keys come from the secrets backend at the moment of use and travel only inside the signed
bundle over mTLS (backend design, "Secrets"); they are never rendered into the HAProxy files, the
revision snapshots or the Git mirror.
"""

import os
from uuid import UUID

from switchyard.adapters.secrets import SecretsReader
from switchyard.config import BROKER_URL_ENV
from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot
from switchyard.render import escape as esc
from switchyard.render.auth import auth_secret_refs, render_auth_files
from switchyard.render.escape import RenderError
from switchyard.render.routing import EngineSlot, render_agent_bundle
from switchyard.render.tokens import jwt_key_files

CERT_PATH = "certs/{name}.pem"
CA_PATH = "certs/ca-{name}.pem"
CRL_PATH = "certs/ca-{name}.crl"


class BundleError(Exception):
    """The files for an engine cannot be built (missing secret, malformed material)."""


def referenced_certs(snapshot: Snapshot, se_group_id: UUID) -> list[dm.Certificate]:
    """Return the certificates the group's enabled virtual services use, sorted by name.

    Example:
        >>> from switchyard.domain.snapshot import Snapshot
        >>> referenced_certs(Snapshot(), UUID(int=1))
        []
    """
    certs = {c.id: c for c in snapshot.certificates}
    wanted: dict[UUID, dm.Certificate] = {}
    for svc in snapshot.virtual_services:
        if svc.se_group_id == se_group_id and svc.enabled:
            for cert_id in svc.certificate_ids:
                if cert_id in certs:
                    wanted[cert_id] = certs[cert_id]
            for port in svc.ports:
                if port.client_ca_id in certs:
                    wanted[port.client_ca_id] = certs[port.client_ca_id]
    for pool in snapshot.pools:
        if pool.client_certificate_id in certs and _pool_in_group(snapshot, pool.id, se_group_id):
            wanted[pool.client_certificate_id] = certs[pool.client_certificate_id]
    return sorted(wanted.values(), key=lambda c: c.name)


def _pool_in_group(snapshot: Snapshot, pool_id: UUID, se_group_id: UUID) -> bool:
    """True when an enabled virtual service of the group routes to the pool."""
    return any(
        s.se_group_id == se_group_id
        and s.enabled
        and (s.default_pool_id == pool_id or any(pool_id in r.pool_ids for r in s.routes))
        for s in snapshot.virtual_services
    )


async def _bgp_secrets(
    snapshot: Snapshot, group: dm.SeGroup, secrets: SecretsReader | None
) -> dict[str, str]:
    refs = {
        peer.md5_ref
        for profile in snapshot.routing_profiles
        if profile.id == group.routing_profile_id and isinstance(profile, dm.BgpProfile)
        for peer in profile.peers
        if peer.md5_ref
    }
    found: dict[str, str] = {}
    for ref in sorted(refs):
        if secrets is None:
            raise BundleError("BGP passwords are configured but no secrets backend is available")
        try:
            found[ref] = await secrets.get(ref)
        except LookupError as exc:
            raise BundleError(f"secret {ref} for BGP peer authentication not found") from exc
    return found


async def _auth_files(
    snapshot: Snapshot, group: dm.SeGroup, secrets: SecretsReader | None
) -> dict[str, str]:
    """The authentication proxy configs of the group's access sets (ADR 0130)."""
    refs = auth_secret_refs(snapshot.for_se_group(group.id), group.id)
    if not refs:
        return {}
    if secrets is None:
        raise BundleError("access policy sets are in use but no secrets backend is available")
    found: dict[str, str] = {}
    for ref in refs:
        try:
            found[ref] = await secrets.get(ref)
        except LookupError as exc:
            raise BundleError(f"secret {ref} for an access policy set not found") from exc
    try:
        return render_auth_files(snapshot, group.id, found, os.environ.get(BROKER_URL_ENV))
    except RenderError as exc:
        raise BundleError(str(exc)) from exc


async def _ca_files(cert: dm.Certificate, secrets: SecretsReader | None) -> dict[str, str]:
    """The CA bundle (certificates only) and the optional revocation list of a client CA."""
    if secrets is None:
        raise BundleError("client CAs are in use but no secrets backend is available")
    name = esc.ident(cert.name)
    try:
        pem = await secrets.get(cert.secret_ref)
    except LookupError as exc:
        raise BundleError(f"secret {cert.secret_ref} for client CA {cert.name} not found") from exc
    if "PRIVATE KEY" in pem or "BEGIN CERTIFICATE" not in pem:
        raise BundleError(f"secret for client CA {cert.name} must hold certificates only")
    files = {CA_PATH.format(name=name): pem if pem.endswith("\n") else pem + "\n"}
    if cert.crl_secret_ref:
        try:
            crl = await secrets.get(cert.crl_secret_ref)
        except LookupError as exc:
            raise BundleError(
                f"secret {cert.crl_secret_ref} for the CRL of {cert.name} not found"
            ) from exc
        if "BEGIN X509 CRL" not in crl:
            raise BundleError(f"secret for the CRL of {cert.name} must hold a PEM CRL")
        files[CRL_PATH.format(name=name)] = crl if crl.endswith("\n") else crl + "\n"
    return files


async def engine_files(
    snapshot: Snapshot, group: dm.SeGroup, slot: EngineSlot, secrets: SecretsReader | None
) -> dict[str, str]:
    """Return ``path -> content`` for one engine (empty if nothing is agent-managed for it).

    Raises:
        BundleError: If a secret is missing or a certificate bundle is not a PEM with a key.
    """
    files: dict[str, str] = {}
    if group.routing_profile_id is not None:
        bgp = await _bgp_secrets(snapshot, group, secrets)
        try:
            files |= render_agent_bundle(snapshot, group.id, slot, bgp)
        except RenderError as exc:
            raise BundleError(str(exc)) from exc
    files |= await _auth_files(snapshot, group, secrets)
    files |= jwt_key_files(snapshot, group.id)
    for cert in referenced_certs(snapshot, group.id):
        if cert.purpose == "client_ca":
            files |= await _ca_files(cert, secrets)
            continue
        if secrets is None:
            raise BundleError("certificates are in use but no secrets backend is available")
        try:
            pem = await secrets.get(cert.secret_ref)
        except LookupError as exc:
            raise BundleError(
                f"secret {cert.secret_ref} for certificate {cert.name} not found"
            ) from exc
        if "PRIVATE KEY" not in pem or "BEGIN CERTIFICATE" not in pem:
            raise BundleError(f"secret for certificate {cert.name} must hold a certificate and key")
        files[CERT_PATH.format(name=esc.ident(cert.name))] = (
            pem if pem.endswith("\n") else pem + "\n"
        )
    return files

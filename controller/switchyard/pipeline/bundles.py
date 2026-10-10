"""The signed file set one engine's agent should hold: routing config plus TLS certificates.

The apply pipeline sends it and the status poller compares against it, so both build it here.
Private keys come from the secrets backend at the moment of use and travel only inside the signed
bundle over mTLS (backend design, "Secrets"); they are never rendered into the HAProxy files, the
revision snapshots or the Git mirror.
"""

from uuid import UUID

from switchyard.adapters.secrets import SecretsReader
from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot
from switchyard.render import escape as esc
from switchyard.render.escape import RenderError
from switchyard.render.routing import EngineSlot, render_agent_bundle

CERT_PATH = "certs/{name}.pem"


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
    return sorted(wanted.values(), key=lambda c: c.name)


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
    for cert in referenced_certs(snapshot, group.id):
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

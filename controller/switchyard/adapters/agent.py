"""The SE agent adapter: signed bundles for FRR, Keepalived and Coraza, and status.

The controller signs each bundle with an Ed25519 key; the agent only accepts bundles that verify
against the public key it was enrolled with, writes only under ``/etc/switchyard/{frr,keepalived,
coraza}/`` and reloads an allowlisted set of units. The transport is gRPC over mutual TLS.
"""

import base64
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import grpc
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from switchyard.adapters.agentpb import agent_pb2, agent_pb2_grpc


class AgentError(Exception):
    """Base class of agent failures."""


class AgentUnreachableError(AgentError):
    """The agent could not be reached."""


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of an applied bundle.

    Attributes:
        ok: True if the bundle was verified, written and its units reloaded.
        error: Why not.
        changed_files: Files that differed from the disk.
        reloaded_units: Units that were reloaded.
        restored_previous: A reload failed and the agent put the previous files back.
    """

    ok: bool
    error: str = ""
    changed_files: tuple[str, ...] = ()
    reloaded_units: tuple[str, ...] = ()
    restored_previous: bool = False


@dataclass(frozen=True)
class BgpSession:
    """One BGP neighbour."""

    peer: str
    state: str
    remote_asn: int


@dataclass(frozen=True)
class VrrpInstance:
    """One VRRP instance."""

    name: str
    state: str


@dataclass(frozen=True)
class IpvsRealServer:
    """One IPVS real server of a UDP virtual service (same member shape as HAProxy's).

    Attributes:
        service: Virtual service name, taken from the rendered ``keepalived.conf``.
        vip: Virtual service address.
        port: Virtual service port.
        address: Real server address.
        member_port: Real server port.
        state: ``up``, ``down`` (configured but not in the IPVS table) or ``drain`` (weight 0).
        weight: Current IPVS weight.
        active_conns: Active connection entries.
        inactive_conns: Inactive connection entries.
        in_packets: Packets from clients.
        out_packets: Packets to clients.
        in_bytes: Bytes from clients.
        out_bytes: Bytes to clients.
    """

    service: str
    vip: str
    port: int
    address: str
    member_port: int
    state: str
    weight: int = 0
    active_conns: int = 0
    inactive_conns: int = 0
    in_packets: int = 0
    out_packets: int = 0
    in_bytes: int = 0
    out_bytes: int = 0


@dataclass(frozen=True)
class AgentStatus:
    """What an agent reports."""

    agent_version: str = ""
    bundle_id: str = ""
    file_hashes: Mapping[str, str] = field(default_factory=dict)
    bgp: tuple[BgpSession, ...] = ()
    vrrp: tuple[VrrpInstance, ...] = ()
    ipvs: tuple[IpvsRealServer, ...] = ()
    haproxy_healthy: bool = False
    routes_withdrawn: bool = False
    observed_at_unix: int = 0


class AgentEngine(Protocol):
    """One SE's agent."""

    @property
    def name(self) -> str:
        """Engine name for logs."""
        ...

    async def apply_bundle(self, bundle_id: str, files: Mapping[str, str]) -> ApplyResult:
        """Sign and send a complete bundle of files (paths relative to ``/etc/switchyard/``)."""
        ...

    async def status(self) -> AgentStatus:
        """Return a status snapshot."""
        ...

    def watch(self) -> AsyncIterator[AgentStatus]:
        """Stream status snapshots as they change."""
        ...

    async def close(self) -> None:
        """Release the connection."""
        ...


class BundleSigner:
    """Signs bundles with an Ed25519 key."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        """Wrap a private key."""
        self._key = private_key

    @classmethod
    def generate(cls) -> "BundleSigner":
        """Create a signer with a fresh key."""
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_pem(cls, pem: str) -> "BundleSigner":
        """Load a PKCS#8 PEM private key (kept in OpenBao; never in the database)."""
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("the bundle signing key must be Ed25519")
        return cls(key)

    def private_pem(self) -> str:
        """Serialise the private key as PEM."""
        return self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    def public_b64(self) -> str:
        """The public key as base64 raw bytes, the form the agent reads from ``bundle.pub``."""
        raw = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return base64.b64encode(raw).decode()

    def sign(self, bundle_id: str, files: Mapping[str, str]) -> tuple[bytes, bytes]:
        """Return ``(serialised bundle, signature)``; files are sorted for determinism."""
        bundle = agent_pb2.Bundle(
            id=bundle_id,
            files=[agent_pb2.File(path=p, content=files[p].encode()) for p in sorted(files)],
        )
        raw = bundle.SerializeToString(deterministic=True)
        return raw, self._key.sign(raw)


def _status(message: agent_pb2.Status) -> AgentStatus:
    return AgentStatus(
        agent_version=message.agent_version,
        bundle_id=message.bundle_id,
        file_hashes=dict(message.file_hashes),
        bgp=tuple(BgpSession(s.peer, s.state, s.remote_asn) for s in message.bgp),
        vrrp=tuple(VrrpInstance(v.name, v.state) for v in message.vrrp),
        ipvs=tuple(
            IpvsRealServer(
                s.service,
                s.vip,
                s.port,
                s.address,
                s.member_port,
                s.state,
                s.weight,
                s.active_conns,
                s.inactive_conns,
                s.in_packets,
                s.out_packets,
                s.in_bytes,
                s.out_bytes,
            )
            for s in message.ipvs
        ),
        haproxy_healthy=message.haproxy_healthy,
        routes_withdrawn=message.routes_withdrawn,
        observed_at_unix=message.observed_at_unix,
    )


class GrpcAgent:
    """:class:`AgentEngine` over gRPC."""

    def __init__(
        self,
        target: str,
        *,
        name: str,
        signer: BundleSigner,
        ca_file: Path | None = None,
        client_cert: tuple[Path, Path] | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        """Connect lazily to ``host:port``.

        Without ``ca_file`` the channel is insecure; only tests do that.
        """
        self._name = name
        self._signer = signer
        self._timeout = timeout_s
        if ca_file is None:
            self._channel = grpc.aio.insecure_channel(target)
        else:
            credentials = grpc.ssl_channel_credentials(
                root_certificates=ca_file.read_bytes(),
                private_key=client_cert[1].read_bytes() if client_cert else None,
                certificate_chain=client_cert[0].read_bytes() if client_cert else None,
            )
            self._channel = grpc.aio.secure_channel(target, credentials)
        self._stub = agent_pb2_grpc.AgentStub(self._channel)  # type: ignore[no-untyped-call]

    @property
    def name(self) -> str:
        """Engine name."""
        return self._name

    async def apply_bundle(self, bundle_id: str, files: Mapping[str, str]) -> ApplyResult:
        """Sign and send ``files``."""
        raw, signature = self._signer.sign(bundle_id, files)
        try:
            response = await self._stub.ApplyBundle(
                agent_pb2.ApplyBundleRequest(bundle=raw, signature=signature),
                timeout=self._timeout,
            )
        except grpc.aio.AioRpcError as exc:
            raise AgentUnreachableError(
                f"{self._name}: {exc.code().name}: {exc.details()}"
            ) from exc
        return ApplyResult(
            ok=response.ok,
            error=response.error,
            changed_files=tuple(response.changed_files),
            reloaded_units=tuple(response.reloaded_units),
            restored_previous=response.restored_previous,
        )

    async def status(self) -> AgentStatus:
        """Return a status snapshot."""
        try:
            return _status(await self._stub.GetStatus(agent_pb2.GetStatusRequest(), timeout=10))
        except grpc.aio.AioRpcError as exc:
            raise AgentUnreachableError(
                f"{self._name}: {exc.code().name}: {exc.details()}"
            ) from exc

    async def watch(self) -> AsyncIterator[AgentStatus]:
        """Stream status snapshots until the connection ends."""
        try:
            async for message in self._stub.Watch(agent_pb2.WatchRequest()):
                yield _status(message)
        except grpc.aio.AioRpcError as exc:
            raise AgentUnreachableError(
                f"{self._name}: {exc.code().name}: {exc.details()}"
            ) from exc

    async def close(self) -> None:
        """Close the channel."""
        await self._channel.close()


class FakeAgent:
    """In-memory agent for tests: applies bundles to a dict and reports what it holds."""

    def __init__(self, name: str = "fake-agent") -> None:
        """Create an empty agent."""
        self._name = name
        self.files: dict[str, str] = {}
        self.bundle_id = ""
        self.bgp: tuple[BgpSession, ...] = ()
        self.vrrp: tuple[VrrpInstance, ...] = ()
        self.ipvs: tuple[IpvsRealServer, ...] = ()
        self.healthy = True
        self.withdrawn = False
        self.fail_next: str | None = None

    @property
    def name(self) -> str:
        """Engine name."""
        return self._name

    async def apply_bundle(self, bundle_id: str, files: Mapping[str, str]) -> ApplyResult:
        """Replace the held files."""
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            return ApplyResult(ok=False, error=error, restored_previous=True)
        changed = tuple(
            sorted(
                {p for p in files if self.files.get(p) != files[p]} | (set(self.files) - set(files))
            )
        )
        self.files, self.bundle_id = dict(files), bundle_id
        return ApplyResult(ok=True, changed_files=changed)

    async def status(self) -> AgentStatus:
        """Report the held state."""
        import hashlib

        return AgentStatus(
            bundle_id=self.bundle_id,
            file_hashes={p: hashlib.sha256(c.encode()).hexdigest() for p, c in self.files.items()},
            bgp=self.bgp,
            vrrp=self.vrrp,
            ipvs=self.ipvs,
            haproxy_healthy=self.healthy,
            routes_withdrawn=self.withdrawn,
        )

    async def watch(self) -> AsyncIterator[AgentStatus]:
        """Yield the current status once."""
        yield await self.status()

    async def close(self) -> None:
        """Nothing to release."""

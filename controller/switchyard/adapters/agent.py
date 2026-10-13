# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The SE agent adapter: signed bundles for FRR, Keepalived and Coraza, and status.

The controller signs each bundle with an Ed25519 key; the agent only accepts bundles that verify
against the public key it was enrolled with, writes only under ``/etc/switchyard/{frr,keepalived,
coraza}/`` and reloads an allowlisted set of units. The transport is gRPC over mutual TLS.
"""

import base64
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
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
    withdrawn_prefixes: tuple[str, ...] = ()
    waf_expected: bool = False
    waf_running: bool = False
    observed_at_unix: int = 0


@dataclass(frozen=True)
class CaptureSpec:
    """One packet capture to run on an engine (ADR 0194).

    Attributes:
        interface: ``any`` or an interface name of the engine.
        protocol: ``""`` (any), ``tcp``, ``udp`` or ``icmp``.
        host: Optional IP address, matched as source or destination.
        port: Optional port (0 for none); not with ``icmp``.
        duration_s: 1 to 60.
        max_packets: 1 to 20000.
        snaplen: Bytes kept per packet, 64 to 262144 (0: the maximum).
    """

    interface: str = "any"
    protocol: str = ""
    host: str = ""
    port: int = 0
    duration_s: int = 10
    max_packets: int = 1000
    snaplen: int = 0


@dataclass(frozen=True)
class CaptureResult:
    """How a capture ended: packets captured, whether the byte cap cut it short, and an error."""

    packets: int = 0
    truncated: bool = False
    error: str | None = None


CaptureSink = Callable[[bytes], Awaitable[None]]


@dataclass(frozen=True)
class ProbeRequest:
    """One health probe to run from an engine against a configured pool member.

    Attributes:
        protocol: ``tcp``, ``http`` or ``udp``.
        address: Member address; the agent refuses anything not in its applied configuration.
        port: Member (or check) port.
        timeout_ms: 100 to 10000.
        http_method: ``GET``, ``HEAD`` or ``OPTIONS`` (http only).
        http_path: Request path (http only).
        http_host: ``Host`` header (http only).
        expect_status: Accepted status codes; empty means 2xx or 3xx.
        udp_payload: Datagram to send (udp only).
        udp_expect: Bytes the reply must contain; empty means any reply.
    """

    protocol: str
    address: str
    port: int
    timeout_ms: int = 3000
    http_method: str = "GET"
    http_path: str = "/"
    http_host: str = ""
    expect_status: tuple[int, ...] = ()
    udp_payload: bytes = b""
    udp_expect: bytes = b""


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a probe."""

    ok: bool
    latency_ms: int
    detail: str
    refused: bool = False


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

    async def probe(self, request: ProbeRequest) -> ProbeResult:
        """Run one health probe from this engine against a configured pool member."""
        ...

    async def capture(self, spec: CaptureSpec, sink: CaptureSink) -> CaptureResult:
        """Run a packet capture on the engine and pass the pcap bytes to ``sink`` in order."""
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
        self._last_sequence = 0

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

    def next_sequence(self) -> int:
        """A bundle sequence that grows with every call: wall-clock milliseconds, never repeated.

        Agents refuse a sequence at or below the last one they applied, so an old signed bundle
        cannot be sent again. Controllers sharing a signing key need clocks that agree to within
        the time between two applies (NTP).
        """
        self._last_sequence = max(self._last_sequence + 1, time.time_ns() // 1_000_000)
        return self._last_sequence

    def sign(
        self,
        bundle_id: str,
        files: Mapping[str, str],
        engine: str = "",
        sequence: int | None = None,
    ) -> tuple[bytes, bytes]:
        """Return ``(serialised bundle, signature)``; files are sorted for determinism.

        The bundle names the ``engine`` it is for and carries a ``sequence`` (a fresh one when not
        given), both under the signature (ADR 0074).
        """
        bundle = agent_pb2.Bundle(
            id=bundle_id,
            files=[agent_pb2.File(path=p, content=files[p].encode()) for p in sorted(files)],
            engine=engine,
            sequence=self.next_sequence() if sequence is None else sequence,
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
        withdrawn_prefixes=tuple(message.withdrawn_prefixes),
        waf_expected=message.waf_expected,
        waf_running=message.waf_running,
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
        raw, signature = self._signer.sign(bundle_id, files, self._name)
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

    async def capture(self, spec: CaptureSpec, sink: CaptureSink) -> CaptureResult:
        """Stream a capture from the agent into ``sink``."""
        result = CaptureResult()
        try:
            call = self._stub.Capture(
                agent_pb2.CaptureRequest(
                    interface=spec.interface,
                    protocol=spec.protocol,
                    host=spec.host,
                    port=spec.port,
                    duration_s=spec.duration_s,
                    max_packets=spec.max_packets,
                    snaplen=spec.snaplen,
                ),
                timeout=spec.duration_s + 15,
            )
            async for chunk in call:
                if chunk.data:
                    await sink(bytes(chunk.data))
                if chunk.done:
                    result = CaptureResult(
                        packets=int(chunk.packets),
                        truncated=chunk.truncated,
                        error=chunk.error or None,
                    )
        except grpc.aio.AioRpcError as exc:
            raise AgentUnreachableError(
                f"{self._name}: {exc.code().name}: {exc.details()}"
            ) from exc
        return result

    async def probe(self, request: ProbeRequest) -> ProbeResult:
        """Ask the agent to run the probe."""
        try:
            response = await self._stub.ProbeMember(
                agent_pb2.ProbeRequest(
                    protocol=request.protocol,
                    address=request.address,
                    port=request.port,
                    timeout_ms=request.timeout_ms,
                    http_method=request.http_method,
                    http_path=request.http_path,
                    http_host=request.http_host,
                    expect_status=list(request.expect_status),
                    udp_payload=request.udp_payload,
                    udp_expect=request.udp_expect,
                ),
                timeout=request.timeout_ms / 1000 + 5,
            )
        except grpc.aio.AioRpcError as exc:
            raise AgentUnreachableError(
                f"{self._name}: {exc.code().name}: {exc.details()}"
            ) from exc
        return ProbeResult(
            ok=response.ok,
            latency_ms=response.latency_ms,
            detail=response.detail,
            refused=response.refused,
        )

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
        self.withdrawn_prefixes: tuple[str, ...] = ()
        self.waf_running = True
        self.probe_result = ProbeResult(ok=True, latency_ms=1, detail="connected")
        self.probes: list[ProbeRequest] = []
        #: Captures asked for, the pcap bytes the fake returns and how the capture ends.
        self.captures: list[CaptureSpec] = []
        self.capture_data = b""
        self.capture_result = CaptureResult()
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
            routes_withdrawn=self.withdrawn or bool(self.withdrawn_prefixes),
            withdrawn_prefixes=self.withdrawn_prefixes,
            waf_expected="coraza/config.yaml" in self.files,
            waf_running=self.waf_running,
        )

    async def capture(self, spec: CaptureSpec, sink: CaptureSink) -> CaptureResult:
        """Record the request, pass ``capture_data`` to the sink and return ``capture_result``."""
        self.captures.append(spec)
        if self.fail_next == "capture":
            self.fail_next = None
            raise AgentUnreachableError(f"{self._name}: injected failure in capture")
        for start in range(0, len(self.capture_data), 4096):
            await sink(self.capture_data[start : start + 4096])
        return self.capture_result

    async def probe(self, request: ProbeRequest) -> ProbeResult:
        """Record the request and return the prepared result."""
        self.probes.append(request)
        return self.probe_result

    async def watch(self) -> AsyncIterator[AgentStatus]:
        """Yield the current status once."""
        yield await self.status()

    async def close(self) -> None:
        """Nothing to release."""

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""TLS-ALPN-01 challenges: make a tenant's engines present the challenge certificate (ADR 0082).

Every HTTP service with a TLS port renders ``alpn h2,http/1.1,acme-tls/1`` on its bind. While a
certificate is validated the controller loads the challenge certificate into each engine's
HAProxy at run time (no reload) and adds it to the crt-list of every TLS service of the SE group,
for the name being validated and for clients that offer ``acme-tls/1``. The CA connects to the VIP
on port 443 with that protocol and reads the certificate.

HAProxy selects the certificate by name, so while the challenge runs *every* handshake for that
name, from browsers too, gets the challenge certificate. A name that is serving traffic is
interrupted for the few seconds validation takes; the request check says so as a warning.
"""

import hashlib
import logging
from uuid import UUID

from switchyard.adapters.haproxy import HaproxyEngine, HaproxyError
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import tenant_session
from switchyard.domain import models as dm
from switchyard.domain.workflow import EngineState
from switchyard.pipeline.engines import EngineDirectory, EngineRecord
from switchyard.render import escape as esc
from switchyard.services.context import Databases

log = logging.getLogger("switchyard.tlsalpn")

#: What the rendered bind lists when the configuration can answer TLS-ALPN-01.
ALPN_TOKEN = "acme-tls/1"  # noqa: S105


class TlsAlpnError(HaproxyError):
    """The engines cannot present the challenge certificate."""


def tls_services(snapshot: object) -> list[dm.VirtualService]:
    """The enabled HTTP services with a TLS listener on port 443."""
    services = getattr(snapshot, "virtual_services", [])
    return [
        s
        for s in services
        if s.enabled
        and s.protocol is dm.Protocol.HTTP
        and any(p.port == 443 and p.tls for p in s.ports)
    ]


def crt_list_path(service: dm.VirtualService) -> str:
    """The rendered path of a service's crt-list."""
    return f"certs/{esc.ident(service.name)}.crtlist"


class EngineTlsAlpnPublisher:
    """Presents TLS-ALPN-01 challenge certificates on the engines of one tenant."""

    def __init__(self, db: Databases, directory: EngineDirectory, tenant_id: UUID) -> None:
        """Create a publisher for a tenant; call :meth:`close` when the order is done."""
        self._db = db
        self._directory = directory
        self._tenant = tenant_id
        self._engines: list[tuple[EngineRecord, HaproxyEngine, list[str]]] | None = None
        self._published: dict[str, str] = {}

    async def _connect(self) -> list[tuple[EngineRecord, HaproxyEngine, list[str]]]:
        if self._engines is not None:
            return self._engines
        async with tenant_session(self._db.app, [self._tenant]) as session:
            snapshot = await load_snapshot(session)
        lists: dict[UUID, list[str]] = {}
        for service in tls_services(snapshot):
            lists.setdefault(service.se_group_id, []).append(crt_list_path(service))
        if not lists:
            raise TlsAlpnError("no virtual service of this tenant serves HTTPS on port 443")
        found: list[tuple[EngineRecord, HaproxyEngine, list[str]]] = []
        self._engines = found  # closed by close() even when a connect below fails
        for group, paths in sorted(lists.items()):
            for record in await self._directory.list_engines(self._tenant, group):
                if record.state is EngineState.ENABLED:
                    found.append((record, await self._directory.connect(record), sorted(paths)))
        if not found:
            raise TlsAlpnError("the SE groups that serve port 443 have no enabled engine")
        for record, engine, _ in found:
            config = await engine.running_config()
            if config is None or ALPN_TOKEN not in config:
                raise TlsAlpnError(
                    f"engine {record.name} is not running a configuration that answers "
                    "TLS-ALPN-01 challenges yet: apply any change set to its SE group first"
                )
        return found

    @staticmethod
    def _name(domain: str) -> str:
        return f"acme-tls-{hashlib.sha256(domain.encode()).hexdigest()[:10]}.pem"

    async def publish(self, domain: str, certificate_pem: str) -> None:
        """Make every engine present ``certificate_pem`` for ``domain`` to ``acme-tls/1``."""
        name = self._name(domain)
        self._published[domain] = name
        for record, engine, paths in await self._connect():
            try:
                await engine.set_challenge_certificate(paths, name, certificate_pem, domain)
            except HaproxyError as exc:
                raise TlsAlpnError(f"engine {record.name}: {exc}") from exc

    async def withdraw(self, domain: str) -> None:
        """Stop presenting it (every engine is tried; failures are logged)."""
        name = self._published.pop(domain, self._name(domain))
        for record, engine, paths in self._engines or []:
            try:
                await engine.remove_challenge_certificate(paths, name)
            except HaproxyError as exc:
                log.warning("engine %s: could not remove %s: %s", record.name, name, exc)

    async def close(self) -> None:
        """Remove anything still published and close the engine connections."""
        for domain in list(self._published):
            await self.withdraw(domain)
        for _, engine, _ in self._engines or []:
            await engine.close()
        self._engines = None

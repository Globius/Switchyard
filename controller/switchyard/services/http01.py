"""HTTP-01 challenges: make a tenant's engines answer ``/.well-known/acme-challenge/<token>``.

The rendered HAProxy configuration of every HTTP service with a port-80 listener has one rule at
the top of its request handling: if the request path is a key of the challenge map
(``haproxy/maps/acme-http01.map``), answer 200 with the map's value. The map is empty. While a
certificate is validated the controller adds the key authorization to the map of every enabled
engine in the SE groups that serve such a service, through the Data Plane API's runtime map calls
(no reload), and removes it again afterwards. The CA may connect to any engine behind the VIP, so
all of them must answer; if one cannot, the order is not started.
"""

import logging
from uuid import UUID

from switchyard.adapters.haproxy import HaproxyEngine, HaproxyError
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import tenant_session
from switchyard.domain import models as dm
from switchyard.domain.workflow import EngineState
from switchyard.pipeline.engines import EngineDirectory, EngineRecord
from switchyard.render.haproxy import ACME_MAP, ACME_MAP_FILE
from switchyard.services.context import Databases

log = logging.getLogger("switchyard.http01")


class Http01Error(HaproxyError):
    """The engines cannot serve the challenge."""


class EngineHttp01Publisher:
    """Publishes HTTP-01 answers on the engines of one tenant."""

    def __init__(self, db: Databases, directory: EngineDirectory, tenant_id: UUID) -> None:
        """Create a publisher for a tenant; call :meth:`close` when the order is done."""
        self._db = db
        self._directory = directory
        self._tenant = tenant_id
        self._engines: list[tuple[EngineRecord, HaproxyEngine]] | None = None
        self._published: list[str] = []

    async def _connect(self) -> list[tuple[EngineRecord, HaproxyEngine]]:
        if self._engines is not None:
            return self._engines
        async with tenant_session(self._db.app, [self._tenant]) as session:
            snapshot = await load_snapshot(session)
        groups = sorted(
            {
                s.se_group_id
                for s in snapshot.virtual_services
                if s.enabled
                and s.protocol is dm.Protocol.HTTP
                and any(p.port == 80 and not p.tls for p in s.ports)
            }
        )
        if not groups:
            raise Http01Error("no virtual service of this tenant listens for HTTP on port 80")
        found: list[tuple[EngineRecord, HaproxyEngine]] = []
        self._engines = found  # closed by close() even when a connect below fails
        for group in groups:
            for record in await self._directory.list_engines(self._tenant, group):
                if record.state is EngineState.ENABLED:
                    found.append((record, await self._directory.connect(record)))
        if not found:
            raise Http01Error("the SE groups that serve port 80 have no enabled engine")
        for record, engine in found:
            config = await engine.running_config()
            if config is None or ACME_MAP not in config:
                raise Http01Error(
                    f"engine {record.name} is not running a configuration that answers HTTP-01 "
                    "challenges yet: apply any change set to its SE group first"
                )
        return found

    async def publish(self, domain: str, path: str, body: str) -> None:
        """Make every engine answer ``path`` with ``body``."""
        for record, engine in await self._connect():
            try:
                await engine.set_map_entry(ACME_MAP_FILE, path, body)
            except HaproxyError as exc:
                raise Http01Error(f"engine {record.name}: {exc}") from exc
        self._published.append(path)

    async def withdraw(self, domain: str, path: str) -> None:
        """Stop answering ``path`` (every engine is tried; failures are logged)."""
        for record, engine in self._engines or []:
            try:
                await engine.remove_map_entry(ACME_MAP_FILE, path)
            except HaproxyError as exc:
                log.warning(
                    "engine %s: could not remove the challenge %s: %s", record.name, path, exc
                )
        if path in self._published:
            self._published.remove(path)

    async def close(self) -> None:
        """Remove anything still published and close the engine connections."""
        for path in list(self._published):
            await self.withdraw("", path)
        for _, engine in self._engines or []:
            await engine.close()
        self._engines = None

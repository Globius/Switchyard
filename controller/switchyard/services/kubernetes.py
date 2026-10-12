"""The Kubernetes connector: clusters a tenant connects, and the sync that keeps objects in step.

A tenant administrator registers a cluster (API URL, CA, service-account token, the SE group and VIP
pool to use). Every few seconds the worker reads the cluster, plans the objects it asks for
(``switchyard.connectors.kubernetes``), compares them with what the tenant has under the cluster's
name prefix and applies the difference as an ordinary change set of ``system:k8s``: validated,
audited, canaried, revertible, and waiting for approval where the tenant requires it. Services get
their VIP written back to ``status.loadBalancer`` once their virtual service is committed.

Only objects named ``k8s-<cluster>--...`` are touched; anything else in the tenant is left alone.
The connector never has two change sets in flight for one cluster.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select

from switchyard.adapters.dns.base import check_endpoint
from switchyard.adapters.kubernetes import HttpKube, Kube, KubeError
from switchyard.adapters.secrets import SecretBackendError
from switchyard.connectors.kubernetes import ClusterSpec, K8sPlan, plan
from switchyard.db import audit_log
from switchyard.db.k8s_models import KubernetesClusterRow
from switchyard.db.ops_models import ChangeSetRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import ipam
from switchyard.domain import models as dm
from switchyard.domain import validators as v
from switchyard.domain.changes import Action, ChangeError, ChangeOp, ObjectType
from switchyard.domain.workflow import ChangeSetState, Role
from switchyard.ids import uuid7
from switchyard.services.acme_providers import check_ca_bundle
from switchyard.services.changesets import ChangeSetService, ValidationFailedError, require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import (
    AlreadyExistsError,
    ConflictError,
    FieldErrors,
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
)

log = logging.getLogger("switchyard.kubernetes")

KUBERNETES_SYNC_JOB = "kubernetes_sync"
KubeFactory = Callable[[str, str, str | None], Kube]
#: Access the token needs, as ``verb/resource/subresource`` with the group.
PERMISSIONS = (
    ("list", "nodes", "", ""),
    ("list", "services", "", ""),
    ("list", "endpointslices", "discovery.k8s.io", ""),
    ("patch", "services", "", "status"),
)


#: Checked only when the Gateway API is installed.
GATEWAY_PERMISSIONS = (
    ("list", "gateways", "gateway.networking.k8s.io", ""),
    ("list", "httproutes", "gateway.networking.k8s.io", ""),
    ("patch", "gateways", "gateway.networking.k8s.io", "status"),
)


def _http_kube(api_url: str, token: str, ca_pem: str | None) -> Kube:
    return HttpKube(api_url, token, ca_pem=ca_pem)


@dataclass(frozen=True)
class ClusterView:
    """A cluster as the API shows it (never the token)."""

    id: UUID
    name: str
    api_url: str
    ca_set: bool
    se_group: str
    vip_pool: str
    mode: str
    class_name: str
    gateway_class: str
    enabled: bool
    waiting_for_change_set: UUID | None
    last_sync_at: datetime | None
    last_sync_ok: bool | None
    last_sync_error: str | None
    last_sync: dict[str, Any] | None
    version: int


@dataclass(frozen=True)
class ClusterTest:
    """What a connection test found."""

    ok: bool
    detail: str
    version: str | None = None
    nodes: int | None = None
    services: int | None = None
    gateway_api: bool | None = None
    permissions: dict[str, bool] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _view(
    row: KubernetesClusterRow, groups: dict[UUID, str], pools: dict[UUID, str]
) -> ClusterView:
    return ClusterView(
        id=row.id,
        name=row.name,
        api_url=row.api_url,
        ca_set=bool(row.ca_pem),
        se_group=groups.get(row.se_group_id, str(row.se_group_id)),
        vip_pool=pools.get(row.vip_pool_id, str(row.vip_pool_id)),
        mode=row.mode,
        class_name=row.class_name,
        gateway_class=row.gateway_class,
        enabled=row.enabled,
        waiting_for_change_set=row.pending_change_set,
        last_sync_at=row.last_sync_at,
        last_sync_ok=row.last_sync_ok,
        last_sync_error=row.last_sync_error,
        last_sync=row.last_sync,
        version=row.version,
    )


class KubernetesClusters:
    """Manage clusters and run the sync."""

    def __init__(
        self,
        db: Databases,
        secrets: Any,
        change_sets: ChangeSetService,
        *,
        kube_factory: KubeFactory = _http_kube,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Create the service.

        Args:
            db: Database access.
            secrets: A backend that reads and writes (OpenBao) for the service-account tokens.
            change_sets: The change set service the sync uses.
            kube_factory: Builds a client from ``(api_url, token, ca_pem)``; replaced in tests.
            clock: Replaceable clock.
        """
        self._db = db
        self._secrets = secrets
        self._change_sets = change_sets
        self._factory = kube_factory
        self._clock = clock

    # -- managing -----------------------------------------------------------------------------

    @staticmethod
    def _check_tenant(principal: Principal, tenant_id: UUID) -> None:
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")

    async def _names(self, tenant_id: UUID) -> tuple[Any, dict[UUID, str], dict[UUID, str]]:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
        return (
            snapshot,
            {g.id: g.name for g in snapshot.se_groups},
            {p.id: p.name for p in snapshot.vip_pools},
        )

    async def list_clusters(self, principal: Principal, tenant_id: UUID) -> list[ClusterView]:
        """The tenant's clusters by name."""
        self._check_tenant(principal, tenant_id)
        require(principal, "read", "tenant_data")
        _, groups, pools = await self._names(tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = (
                (
                    await session.execute(
                        select(KubernetesClusterRow).order_by(KubernetesClusterRow.name)
                    )
                )
                .scalars()
                .all()
            )
        return [_view(r, groups, pools) for r in rows]

    async def get(self, principal: Principal, tenant_id: UUID, cluster_id: UUID) -> ClusterView:
        """One cluster."""
        self._check_tenant(principal, tenant_id)
        require(principal, "read", "tenant_data")
        _, groups, pools = await self._names(tenant_id)
        return _view(await self._row(tenant_id, cluster_id), groups, pools)

    async def _row(self, tenant_id: UUID, cluster_id: UUID) -> KubernetesClusterRow:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await session.get(KubernetesClusterRow, cluster_id)
        if row is None:
            raise NotFoundError(f"Kubernetes cluster {cluster_id} does not exist")
        return row

    @staticmethod
    def _check_fields(
        errors: FieldErrors,
        *,
        api_url: str,
        ca_pem: str | None,
        mode: str,
        class_name: str,
        gateway_class: str,
    ) -> None:
        parsed = urlparse(api_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.fragment:
            errors.add(
                "api_url", "invalid", "must be an https URL such as https://k8s.example.test:6443"
            )
        else:
            try:
                check_endpoint(parsed.hostname, "api_url", env="SWITCHYARD_K8S_CIDRS")
            except ValueError as exc:
                errors.add("api_url", "forbidden", str(exc))
        if ca_pem and (problem := check_ca_bundle(ca_pem)):
            errors.add("ca_pem", "invalid", problem)
        if mode not in {"nodeport", "pods"}:
            errors.add("mode", "invalid", "must be nodeport or pods")
        for key, value in (("class_name", class_name), ("gateway_class", gateway_class)):
            if not value or len(value) > 63 or any(c.isspace() for c in value):
                errors.add(key, "invalid", "must be a non-empty name without spaces")

    async def create(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        name: str,
        api_url: str,
        token: str,
        se_group: str,
        vip_pool: str,
        ca_pem: str | None = None,
        mode: str = "nodeport",
        class_name: str = "switchyard.io/lb",
        gateway_class: str = "switchyard",
        enabled: bool = True,
        request_id: str | None = None,
    ) -> ClusterView:
        """Register a cluster and store its token.

        Raises:
            FieldErrors: With the path of every invalid field.
            AlreadyExistsError: If the name is taken.
        """
        self._check_tenant(principal, tenant_id)
        require(principal, "manage", "kubernetes_cluster")
        errors = FieldErrors()
        try:
            v.validate_name(name)
        except ValueError as exc:
            errors.add("name", "invalid", str(exc))
        self._check_fields(
            errors,
            api_url=api_url,
            ca_pem=ca_pem,
            mode=mode,
            class_name=class_name,
            gateway_class=gateway_class,
        )
        if not token.strip():
            errors.add("token", "required", "the service-account token is required")
        snapshot, groups, pools = await self._names(tenant_id)
        group = next((g for g in snapshot.se_groups if g.name == se_group), None)
        pool = next((p for p in snapshot.vip_pools if p.name == vip_pool), None)
        if group is None:
            errors.add("se_group", "not_found", f"no SE group named {se_group}")
        if pool is None:
            errors.add("vip_pool", "not_found", f"no VIP pool named {vip_pool}")
        errors.raise_if_any()
        tenant_name = await self._tenant_name(tenant_id)
        ref = f"kv/tenants/{tenant_name}/k8s/{name}"
        try:
            await self._secrets.put(ref, token.strip())
        except SecretBackendError as exc:
            raise ConflictError(f"could not store the token: {exc}") from exc
        async with tenant_session(self._db.app, [tenant_id]) as session:
            if (
                await session.execute(
                    select(KubernetesClusterRow.id).where(KubernetesClusterRow.name == name)
                )
            ).first():
                raise AlreadyExistsError(f"Kubernetes cluster {name} already exists")
            row = KubernetesClusterRow(
                id=uuid7(),
                tenant_id=tenant_id,
                name=name,
                api_url=api_url.rstrip("/"),
                ca_pem=ca_pem or None,
                token_ref=ref,
                se_group_id=group.id,  # type: ignore[union-attr]
                vip_pool_id=pool.id,  # type: ignore[union-attr]
                mode=mode,
                class_name=class_name,
                gateway_class=gateway_class,
                enabled=enabled,
            )
            session.add(row)
            await session.flush()
            view = _view(row, groups, pools)
        await self._audit(
            principal.actor,
            tenant_id,
            "kubernetes_cluster.created",
            name,
            {"api_url": api_url},
            request_id,
        )
        return view

    async def update(
        self,
        principal: Principal,
        tenant_id: UUID,
        cluster_id: UUID,
        *,
        expected_version: int | None = None,
        api_url: str | None = None,
        token: str | None = None,
        ca_pem: str | None = None,
        se_group: str | None = None,
        vip_pool: str | None = None,
        mode: str | None = None,
        class_name: str | None = None,
        gateway_class: str | None = None,
        enabled: bool | None = None,
        request_id: str | None = None,
    ) -> ClusterView:
        """Change a cluster; omitted fields (and an omitted token) stay as they are."""
        self._check_tenant(principal, tenant_id)
        require(principal, "manage", "kubernetes_cluster")
        current = await self._row(tenant_id, cluster_id)
        if expected_version is not None and current.version != expected_version:
            raise PreconditionFailedError(
                f"cluster {current.name} is at version {current.version}, not {expected_version}"
            )
        errors = FieldErrors()
        self._check_fields(
            errors,
            api_url=api_url or current.api_url,
            ca_pem=ca_pem if ca_pem is not None else current.ca_pem,
            mode=mode or current.mode,
            class_name=class_name or current.class_name,
            gateway_class=gateway_class or current.gateway_class,
        )
        snapshot, groups, pools = await self._names(tenant_id)
        group = (
            next((g for g in snapshot.se_groups if g.name == se_group), None) if se_group else None
        )
        pool = (
            next((p for p in snapshot.vip_pools if p.name == vip_pool), None) if vip_pool else None
        )
        if se_group and group is None:
            errors.add("se_group", "not_found", f"no SE group named {se_group}")
        if vip_pool and pool is None:
            errors.add("vip_pool", "not_found", f"no VIP pool named {vip_pool}")
        errors.raise_if_any()
        if token and token.strip():
            try:
                await self._secrets.put(current.token_ref, token.strip())
            except SecretBackendError as exc:
                raise ConflictError(f"could not store the token: {exc}") from exc
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await session.get(KubernetesClusterRow, cluster_id, with_for_update=True)
            if row is None:
                raise NotFoundError(f"Kubernetes cluster {cluster_id} does not exist")
            if api_url:
                row.api_url = api_url.rstrip("/")
            if ca_pem is not None:
                row.ca_pem = ca_pem or None
            if group is not None:
                row.se_group_id = group.id
            if pool is not None:
                row.vip_pool_id = pool.id
            if mode:
                row.mode = mode
            if class_name:
                row.class_name = class_name
            if gateway_class:
                row.gateway_class = gateway_class
            if enabled is not None:
                row.enabled = enabled
            row.version += 1
            await session.flush()
            view = _view(row, groups, pools)
        await self._audit(
            principal.actor,
            tenant_id,
            "kubernetes_cluster.updated",
            current.name,
            {"token_changed": bool(token)},
            request_id,
        )
        return view

    async def delete(
        self,
        principal: Principal,
        tenant_id: UUID,
        cluster_id: UUID,
        *,
        expected_version: int | None = None,
        request_id: str | None = None,
    ) -> None:
        """Remove a cluster. Its objects stay until the cluster is gone from the tenant by hand."""
        self._check_tenant(principal, tenant_id)
        require(principal, "manage", "kubernetes_cluster")
        current = await self._row(tenant_id, cluster_id)
        if expected_version is not None and current.version != expected_version:
            raise PreconditionFailedError(
                f"cluster {current.name} is at version {current.version}, not {expected_version}"
            )
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await session.get(KubernetesClusterRow, cluster_id)
            if row is not None:
                await session.delete(row)
        await self._audit(
            principal.actor, tenant_id, "kubernetes_cluster.deleted", current.name, {}, request_id
        )

    async def _tenant_name(self, tenant_id: UUID) -> str:
        from switchyard.db.models import TenantRow

        async with admin_session(self._db.admin) as session:
            name = (
                await session.execute(select(TenantRow.name).where(TenantRow.id == tenant_id))
            ).scalar_one_or_none()
        if name is None:
            raise NotFoundError(f"tenant {tenant_id} does not exist")
        return str(name)

    async def _audit(
        self,
        actor: str,
        tenant_id: UUID,
        action: str,
        name: str,
        diff: dict[str, Any],
        request_id: str | None,
    ) -> None:
        async with admin_session(self._db.admin) as session:
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=actor,
                action=action,
                object_ref=f"kubernetes_cluster:{name}",
                diff=diff,
                request_id=request_id,
            )

    # -- testing ------------------------------------------------------------------------------

    async def test(
        self,
        principal: Principal,
        tenant_id: UUID,
        cluster_id: UUID,
        *,
        request_id: str | None = None,
    ) -> ClusterTest:
        """Reach the cluster and check what the connector needs: version, objects, access."""
        self._check_tenant(principal, tenant_id)
        require(principal, "manage", "kubernetes_cluster")
        row = await self._row(tenant_id, cluster_id)
        kube: Kube | None = None
        try:
            token = await self._secrets.get(row.token_ref)
            kube = self._factory(row.api_url, token, row.ca_pem)
            version = await kube.version()
            nodes = await kube.nodes()
            services = await kube.services()
            gateways = await kube.gateways()
            permissions = {}
            for verb, resource, group, sub in PERMISSIONS + (
                GATEWAY_PERMISSIONS if gateways is not None else ()
            ):
                permissions[f"{verb} {resource}{'/' + sub if sub else ''}"] = await kube.can(
                    verb, resource, group, sub
                )
            problems = [f"the token may not {k}" for k, ok in permissions.items() if not ok]
            ready = sum(
                1
                for n in nodes
                if any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (n.get("status") or {}).get("conditions") or []
                )
            )
            if row.mode == "nodeport" and not ready:
                problems.append("no node is Ready")
            result = ClusterTest(
                not problems,
                "reachable, with the access the connector needs"
                if not problems
                else "; ".join(problems),
                version,
                len(nodes),
                len(services),
                gateways is not None,
                permissions,
                problems,
            )
        except Exception as exc:
            result = ClusterTest(False, f"{type(exc).__name__}: {exc}", problems=[str(exc)])
        finally:
            if kube is not None:
                await kube.close()
        await self._audit(
            principal.actor,
            tenant_id,
            "kubernetes_cluster.tested",
            row.name,
            {"ok": result.ok},
            request_id,
        )
        return result

    async def request_sync(
        self, principal: Principal, tenant_id: UUID, *, request_id: str | None = None
    ) -> UUID:
        """Queue a sync now (the worker also runs one every 30 seconds)."""
        from switchyard.jobs import queue

        self._check_tenant(principal, tenant_id)
        require(principal, "manage", "kubernetes_cluster")
        async with admin_session(self._db.admin) as session:
            return await queue.enqueue(
                session,
                KUBERNETES_SYNC_JOB,
                {"tenant_id": str(tenant_id)},
                tenant_id=tenant_id,
                max_attempts=1,
                request_id=request_id,
            )

    # -- syncing ------------------------------------------------------------------------------

    async def sync_all(self) -> list[str]:
        """Sync every enabled cluster (the worker's job). A failure is recorded, not raised."""
        async with admin_session(self._db.admin) as session:
            rows = (
                (
                    await session.execute(
                        select(KubernetesClusterRow).where(KubernetesClusterRow.enabled)
                    )
                )
                .scalars()
                .all()
            )
            todo = [(r.tenant_id, r.id, r.name) for r in rows]
        done = []
        for tenant_id, cluster_id, name in todo:
            try:
                await self.sync(tenant_id, cluster_id)
                done.append(name)
            except Exception as exc:
                log.warning("syncing cluster %s failed: %s", name, exc)
                await self._record(cluster_id, ok=False, error=str(exc) or type(exc).__name__)
        return done

    async def _record(
        self,
        cluster_id: UUID,
        *,
        ok: bool,
        error: str | None = None,
        summary: dict[str, Any] | None = None,
        pending: UUID | bool | None = False,
    ) -> None:
        async with admin_session(self._db.admin) as session:
            row = await session.get(KubernetesClusterRow, cluster_id)
            if row is None:
                return
            row.last_sync_at = self._clock()
            row.last_sync_ok = ok
            row.last_sync_error = error
            if summary is not None:
                row.last_sync = _with_last_change(summary, row.last_sync, row.last_sync_at)
            if pending is not False:
                row.pending_change_set = pending if isinstance(pending, UUID) else None

    async def sync(self, tenant_id: UUID, cluster_id: UUID) -> dict[str, Any]:
        """Read the cluster, plan, and apply the difference as one change set. Returns a summary."""
        row = await self._row(tenant_id, cluster_id)
        if row.pending_change_set is not None:
            state = await self._change_set_state(row.pending_change_set)
            if state in {
                ChangeSetState.DRAFT,
                ChangeSetState.APPLYING,
                ChangeSetState.PENDING_APPROVAL,
            }:
                return {
                    "skipped": f"waiting for change set {row.pending_change_set} ({state.value})"
                }
            await self._record(
                cluster_id,
                ok=state is ChangeSetState.APPLIED,
                error=None
                if state is ChangeSetState.APPLIED
                else f"the last change set {state.value}",
                pending=None,
            )
        token = await self._secrets.get(row.token_ref)
        kube = self._factory(row.api_url, token, row.ca_pem)
        try:
            nodes = await kube.nodes()
            services = await kube.services()
            slices = await kube.endpoint_slices()
            gateways = await kube.gateways()
            routes = await kube.http_routes()
            spec = ClusterSpec(row.name, row.mode, row.class_name, row.gateway_class)  # type: ignore[arg-type]
            planned = plan(spec, nodes, services, slices, gateways, routes)
            snapshot, _, _ = await self._names(tenant_id)
            ops, vips, summary = self._diff(spec, planned, snapshot, row)
            summary["notes"] = [
                f"{n.object}: {n.message}" for n in planned.notes if n.level == "warning"
            ][:50]
            change_set: UUID | None = None
            if ops:
                change_set = await self._submit(tenant_id, row.name, ops)
                summary["change_set_id"] = str(change_set)
            written = await self._write_back(kube, planned, snapshot, vips, services)
            written += await self._write_gateways(kube, planned, snapshot, vips, gateways or [])
            summary["addresses_written"] = written
        finally:
            await kube.close()
        await self._record(
            cluster_id, ok=True, summary=summary, pending=change_set if ops else False
        )
        return summary

    async def _change_set_state(self, change_set_id: UUID) -> ChangeSetState:
        async with admin_session(self._db.admin) as session:
            record = await session.get(ChangeSetRow, change_set_id)
        return ChangeSetState(record.state) if record else ChangeSetState.DISCARDED

    def _diff(
        self, spec: ClusterSpec, planned: K8sPlan, snapshot: Any, row: KubernetesClusterRow
    ) -> tuple[list[ChangeOp], dict[str, str], dict[str, Any]]:
        prefix = spec.prefix
        pools = {p.name: p for p in snapshot.pools if p.name.startswith(prefix)}
        services = {s.name: s for s in snapshot.virtual_services if s.name.startswith(prefix)}
        monitors = {m.name: m for m in snapshot.health_monitors if m.name.startswith(prefix)}
        certs = {c.name: c for c in snapshot.certificates}
        pool_obj = next((p for p in snapshot.vip_pools if p.id == row.vip_pool_id), None)
        network = next(
            (n for n in snapshot.networks if pool_obj and n.id == pool_obj.network_id), None
        )

        # one VIP per Kubernetes Service: keep what exists, allocate for the rest
        vips: dict[str, str] = {}
        used: list[Any] = [s.vip for s in snapshot.virtual_services]
        for name, sp in planned.services.items():
            if name in services and sp.vip_key not in vips:
                vips[sp.vip_key] = str(services[name].vip)
        for sp in planned.services.values():
            if sp.vip_key in vips:
                continue
            if pool_obj is None or network is None:
                continue
            try:
                allocated = ipam.allocate(pool_obj, network, used)
            except ValueError:
                planned.note("warning", sp.name, "the VIP pool has no free address")
                continue
            used.append(allocated)
            vips[sp.vip_key] = str(allocated)

        creates_m: list[ChangeOp] = []
        creates_p: list[ChangeOp] = []
        updates_p: list[ChangeOp] = []
        creates_s: list[ChangeOp] = []
        updates_s: list[ChangeOp] = []
        deletes_s: list[ChangeOp] = []
        deletes_p: list[ChangeOp] = []
        deletes_m: list[ChangeOp] = []

        udp_pools = {
            sp.default_pool
            for sp in planned.services.values()
            if sp.protocol == "udp" and sp.default_pool
        }
        monitor_ids: dict[str, UUID] = {}
        for kind in ("tcp", "udp"):
            wanted = any((name in udp_pools) == (kind == "udp") for name in planned.pools)
            if not wanted:
                continue
            mon = monitors.get(f"{prefix}{kind}")
            if mon is not None:
                monitor_ids[kind] = mon.id
                continue
            monitor_ids[kind] = uuid7()
            creates_m.append(
                ChangeOp(
                    object_type=ObjectType.HEALTH_MONITOR,
                    object_id=monitor_ids[kind],
                    action=Action.CREATE,
                    body={
                        "name": f"{prefix}{kind}",
                        "type": kind,
                        "interval_s": 5,
                        "timeout_s": 3,
                        "rise": 2,
                        "fall": 3,
                    },
                )
            )
        pool_ids: dict[str, UUID] = {}
        for name, pp in planned.pools.items():
            existing = pools.get(name)
            if existing is None:
                pool_ids[name] = uuid7()
                creates_p.append(
                    ChangeOp(
                        object_type=ObjectType.POOL,
                        object_id=pool_ids[name],
                        action=Action.CREATE,
                        body={
                            "name": name,
                            "members": [
                                {"id": str(uuid7()), "address": a, "port": p} for a, p in pp.members
                            ],
                            "health_monitor_id": str(
                                monitor_ids["udp" if name in udp_pools else "tcp"]
                            ),
                        },
                    )
                )
                continue
            pool_ids[name] = existing.id
            have = sorted((str(m.address), m.port) for m in existing.members)
            if have != sorted(pp.members):
                ids = {(str(m.address), m.port): m.id for m in existing.members}
                updates_p.append(
                    ChangeOp(
                        object_type=ObjectType.POOL,
                        object_id=existing.id,
                        action=Action.UPDATE,
                        body={
                            "members": [
                                {"id": str(ids.get((a, p), uuid7())), "address": a, "port": p}
                                for a, p in pp.members
                            ]
                        },
                        expected_version=existing.version,
                    )
                )
        for name, sp in planned.services.items():
            address = vips.get(sp.vip_key)
            if address is None:
                continue
            body: dict[str, Any] = {
                "se_group_id": str(row.se_group_id),
                "protocol": sp.protocol,
                "enabled": True,
                "vip": address,
                "vip_pool_id": str(row.vip_pool_id),
                "ports": sp.ports,
            }
            if sp.default_pool:
                body["default_pool_id"] = str(pool_ids[sp.default_pool])
            if sp.routes:
                body["routes"] = [
                    {"order": i, "match": r["match"], "pool_id": str(pool_ids[r["pool"]])}
                    for i, r in enumerate(sp.routes)
                ]
            if sp.certificate:
                cert = certs.get(sp.certificate)
                if cert is None:
                    planned.note("warning", name, f"certificate {sp.certificate} does not exist")
                    continue
                body["certificate_ids"] = [str(cert.id)]
            existing_s = services.get(name)
            if existing_s is None:
                creates_s.append(
                    ChangeOp(
                        object_type=ObjectType.VIRTUAL_SERVICE,
                        object_id=uuid7(),
                        action=Action.CREATE,
                        body={"name": name} | body,
                    )
                )
            elif _service_differs(existing_s, body, pool_ids):
                updates_s.append(
                    ChangeOp(
                        object_type=ObjectType.VIRTUAL_SERVICE,
                        object_id=existing_s.id,
                        action=Action.UPDATE,
                        body=body,
                        expected_version=existing_s.version,
                    )
                )
        for name, existing_s in services.items():
            if name not in planned.services:
                deletes_s.append(
                    ChangeOp(
                        object_type=ObjectType.VIRTUAL_SERVICE,
                        object_id=existing_s.id,
                        action=Action.DELETE,
                        expected_version=existing_s.version,
                    )
                )
        for name, existing_p in pools.items():
            if name not in planned.pools:
                deletes_p.append(
                    ChangeOp(
                        object_type=ObjectType.POOL,
                        object_id=existing_p.id,
                        action=Action.DELETE,
                        expected_version=existing_p.version,
                    )
                )
        for kind, mon in ((k, monitors.get(f"{prefix}{k}")) for k in ("tcp", "udp")):
            if mon is not None and kind not in monitor_ids:
                deletes_m.append(
                    ChangeOp(
                        object_type=ObjectType.HEALTH_MONITOR,
                        object_id=mon.id,
                        action=Action.DELETE,
                        expected_version=mon.version,
                    )
                )
        ops = (
            creates_m
            + creates_p
            + updates_p
            + creates_s
            + updates_s
            + deletes_s
            + deletes_p
            + deletes_m
        )
        summary: dict[str, Any] = {
            "planned_services": len(planned.services),
            "planned_pools": len(planned.pools),
            "created": len(creates_s) + len(creates_p),
            "updated": len(updates_s) + len(updates_p),
            "deleted": len(deletes_s) + len(deletes_p),
        }
        return ops, vips, summary

    async def _submit(self, tenant_id: UUID, cluster: str, ops: list[ChangeOp]) -> UUID:
        who = Principal("system:k8s", Role.OPERATOR, frozenset({tenant_id}))
        opened = await self._change_sets.open(who, tenant_id, f"Kubernetes cluster {cluster}: sync")
        change_set_id = opened.record.id
        try:
            await self._change_sets.add_ops(who, tenant_id, change_set_id, ops)
        except (ChangeError, ValidationFailedError) as exc:
            try:
                await self._change_sets.discard(who, tenant_id, change_set_id)
            except Exception:
                log.warning("could not discard change set %s", change_set_id)
            raise ConflictError(f"the planned objects were rejected: {exc}") from exc
        try:
            await self._change_sets.apply(who, tenant_id, change_set_id)
        except PermissionDeniedError:
            await self._change_sets.request_approval(who, tenant_id, change_set_id)
            log.warning("change set %s waits for approval (cluster %s)", change_set_id, cluster)
        return change_set_id

    async def _write_back(
        self,
        kube: Kube,
        planned: K8sPlan,
        snapshot: Any,
        vips: dict[str, str],
        services: list[dict[str, Any]],
    ) -> int:
        """Write VIPs to the Services whose virtual service is already committed."""
        committed = {s.name: str(s.vip) for s in snapshot.virtual_services}
        current = {
            (s["metadata"]["namespace"], s["metadata"]["name"]): next(
                (
                    i.get("ip")
                    for i in ((s.get("status") or {}).get("loadBalancer") or {}).get("ingress")
                    or []
                ),
                None,
            )
            for s in services
        }
        written = 0
        for key, (namespace, name) in planned.status_targets.items():
            wanted = vips.get(key)
            live = [
                sp for sp in planned.services.values() if sp.vip_key == key and sp.name in committed
            ]
            if wanted is None or not live or committed[live[0].name] != wanted:
                continue
            if current.get((namespace, name)) != wanted:
                try:
                    await kube.set_service_address(namespace, name, wanted)
                    written += 1
                except KubeError as exc:
                    log.warning("could not write the address of %s/%s: %s", namespace, name, exc)
        return written

    async def _write_gateways(
        self,
        kube: Kube,
        planned: K8sPlan,
        snapshot: Any,
        vips: dict[str, str],
        gateways: list[dict[str, Any]],
    ) -> int:
        """Mark the Gateways whose virtual service is committed: address, Accepted, Programmed."""
        committed = {s.name: str(s.vip) for s in snapshot.virtual_services}
        by_key = {(g["metadata"]["namespace"], g["metadata"]["name"]): g for g in gateways}
        written = 0
        for key, (namespace, name) in planned.gateway_targets.items():
            wanted = vips.get(key)
            live = [
                sp for sp in planned.services.values() if sp.vip_key == key and sp.name in committed
            ]
            gateway = by_key.get((namespace, name))
            if wanted is None or not live or committed[live[0].name] != wanted or gateway is None:
                continue
            status = gateway.get("status") or {}
            have = [a.get("value") for a in status.get("addresses") or []]
            programmed = any(
                c.get("type") == "Programmed" and c.get("status") == "True"
                for c in status.get("conditions") or []
            )
            generation = int((gateway.get("metadata") or {}).get("generation") or 1)
            observed = all(
                int(c.get("observedGeneration") or generation) == generation
                for c in status.get("conditions") or []
            )
            if have == [wanted] and programmed and observed:
                continue
            try:
                await kube.set_gateway_status(namespace, name, wanted, generation)
                written += 1
            except KubeError as exc:
                log.warning("could not write the status of gateway %s/%s: %s", namespace, name, exc)
        return written


def _with_last_change(
    summary: dict[str, Any], previous: dict[str, Any] | None, at: datetime | None
) -> dict[str, Any]:
    """Keep the counts of the last sync that changed something next to the latest summary.

    A sync that changes nothing replaces ``last_sync``; without this the counts of the sync that
    created the objects would be gone a few seconds later.
    """
    keys = ("created", "updated", "deleted")
    if any(summary.get(k) for k in keys):
        return {
            **summary,
            "last_change": {
                **{k: summary.get(k, 0) for k in keys},
                "change_set_id": summary.get("change_set_id"),
                "at": at.isoformat() if at else None,
            },
        }
    kept = (previous or {}).get("last_change")
    return {**summary, "last_change": kept} if kept else summary


def _service_differs(
    existing: dm.VirtualService, body: dict[str, Any], pools: dict[str, UUID]
) -> bool:
    """Whether the planned body changes anything the connector manages."""
    ports = sorted((p.port, p.tls) for p in existing.ports)
    wanted_ports = sorted((p["port"], bool(p.get("tls"))) for p in body["ports"])
    routes = [
        (r.order, r.match.host, r.match.path_prefix, r.match.path_exact, str(r.pool_id))
        for r in existing.routes
    ]
    wanted_routes = [
        (
            r["order"],
            r["match"].get("host"),
            r["match"].get("path_prefix"),
            r["match"].get("path_exact"),
            r["pool_id"],
        )
        for r in body.get("routes", [])
    ]
    return (
        existing.protocol.value != body["protocol"]
        or str(existing.vip) != body["vip"]
        or ports != wanted_ports
        or (str(existing.default_pool_id) if existing.default_pool_id else None)
        != body.get("default_pool_id")
        or routes != wanted_routes
        or [str(c) for c in existing.certificate_ids] != body.get("certificate_ids", [])
        or not existing.enabled
    )

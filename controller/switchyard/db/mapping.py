"""Convert between domain objects and ORM rows.

The domain layer never sees SQLAlchemy and the ORM never leaks out of ``switchyard.db``. Child
tables (ports, routes, members, ...) keep list order in a ``position`` column.
"""

import ipaddress
from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import DeclarativeBase

from switchyard.db import models as m
from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot


def _ip(value: object) -> str:
    """Return the bare address of a PostgreSQL ``inet`` value, however the driver typed it."""
    return str(ipaddress.ip_interface(str(value)).ip)


def _net(value: object) -> str:
    return str(ipaddress.ip_network(str(value)))


def snapshot_to_rows(snapshot: Snapshot) -> list[DeclarativeBase]:
    """Return ORM rows for every object in the snapshot, in foreign-key dependency order."""
    rows: list[DeclarativeBase] = []
    for t in snapshot.tenants:
        rows.append(
            m.TenantRow(
                id=t.id,
                version=t.version,
                name=t.name,
                oidc_group=t.oidc_group,
                quotas=t.quotas,
                require_approval=t.require_approval,
            )
        )
    for n in snapshot.networks:
        rows.append(
            m.NetworkRow(
                id=n.id,
                version=n.version,
                tenant_id=n.tenant_id,
                name=n.name,
                cidr=str(n.cidr),
                gateway=str(n.gateway) if n.gateway else None,
                sdn_zone=n.sdn_zone,
                vnet=n.vnet,
                vlan_or_vni=n.vlan_or_vni,
                managed=n.managed,
            )
        )
    for vp in snapshot.vip_pools:
        rows.append(
            m.VipPoolRow(
                id=vp.id,
                version=vp.version,
                tenant_id=vp.tenant_id,
                name=vp.name,
                network_id=vp.network_id,
                ranges=[{"start": str(r.start), "end": str(r.end)} for r in vp.ranges],
            )
        )
    for g in snapshot.se_groups:
        rows.append(
            m.SeGroupRow(
                id=g.id,
                version=g.version,
                tenant_id=g.tenant_id,
                name=g.name,
                ha_mode=g.ha_mode.value,
                maxconn=g.maxconn,
                canary_seconds=g.canary_seconds,
            )
        )
    for hm in snapshot.health_monitors:
        rows.append(
            m.HealthMonitorRow(
                id=hm.id,
                version=hm.version,
                tenant_id=hm.tenant_id,
                name=hm.name,
                type=hm.type.value,
                port=hm.port,
                interval_s=hm.interval_s,
                timeout_s=hm.timeout_s,
                rise=hm.rise,
                fall=hm.fall,
                http=hm.http.model_dump(mode="json") if hm.http else None,
            )
        )
    for c in snapshot.certificates:
        rows.append(
            m.CertificateRow(
                id=c.id,
                version=c.version,
                tenant_id=c.tenant_id,
                name=c.name,
                source=c.source.value,
                sans=list(c.sans),
                not_after=c.not_after,
                secret_ref=c.secret_ref,
                renew_policy=c.renew_policy.value,
            )
        )
    for ps in snapshot.policy_sets:
        if isinstance(ps, dm.RedirectPolicySet):
            rows.append(
                m.PolicySetRow(
                    id=ps.id,
                    version=ps.version,
                    tenant_id=ps.tenant_id,
                    name=ps.name,
                    kind="redirect",
                    rules=[r.model_dump(mode="json") for r in ps.rules],
                )
            )
        else:
            rows.append(
                m.PolicySetRow(
                    id=ps.id,
                    version=ps.version,
                    tenant_id=ps.tenant_id,
                    name=ps.name,
                    kind="allow_list",
                    cidrs=[str(c) for c in ps.cidrs],
                    managed_by=ps.managed_by,
                )
            )
    for p in snapshot.pools:
        rows.append(
            m.PoolRow(
                id=p.id,
                version=p.version,
                tenant_id=p.tenant_id,
                name=p.name,
                algorithm=p.algorithm.value,
                persistence=p.persistence.model_dump(mode="json") if p.persistence else None,
                health_monitor_id=p.health_monitor_id,
                tls=p.tls,
                tls_verify=p.tls_verify,
            )
        )
    for s in snapshot.virtual_services:
        rows.append(
            m.VirtualServiceRow(
                id=s.id,
                version=s.version,
                tenant_id=s.tenant_id,
                name=s.name,
                se_group_id=s.se_group_id,
                protocol=s.protocol.value,
                enabled=s.enabled,
                vip=str(s.vip),
                vip_pool_id=s.vip_pool_id,
                default_pool_id=s.default_pool_id,
                allow_list_id=s.allow_list_id,
                enforce_allow_list=s.enforce_allow_list,
                restore_client_ip_header=s.restore_client_ip_header,
            )
        )
    # Child rows come after their parents so inserts satisfy the foreign keys.
    for g in snapshot.se_groups:
        for i, gn in enumerate(g.networks):
            rows.append(
                m.SeGroupNetworkRow(
                    tenant_id=g.tenant_id,
                    se_group_id=g.id,
                    network_id=gn.network_id,
                    role=gn.role.value,
                    position=i,
                )
            )
    for p in snapshot.pools:
        for i, mem in enumerate(p.members):
            rows.append(
                m.PoolMemberRow(
                    id=mem.id,
                    tenant_id=p.tenant_id,
                    pool_id=p.id,
                    position=i,
                    name=mem.name,
                    address=str(mem.address),
                    port=mem.port,
                    weight=mem.weight,
                    admin_state=mem.admin_state.value,
                )
            )
    for s in snapshot.virtual_services:
        for port in s.ports:
            rows.append(
                m.VsPortRow(
                    tenant_id=s.tenant_id,
                    vs_id=s.id,
                    port=port.port,
                    tls=port.tls,
                    http2=port.http2,
                    proxy_protocol=port.proxy_protocol,
                    https_redirect_port=port.https_redirect_port,
                )
            )
        for i, r in enumerate(s.routes):
            rows.append(
                m.VsRouteRow(
                    tenant_id=s.tenant_id,
                    vs_id=s.id,
                    position=i,
                    route_order=r.order,
                    host=r.match.host,
                    path_prefix=r.match.path_prefix,
                    path_exact=r.match.path_exact,
                    pool_id=r.pool_id,
                    redirect_location=r.redirect.location if r.redirect else None,
                    redirect_code=r.redirect.code if r.redirect else None,
                )
            )
        for i, cid in enumerate(s.certificate_ids):
            rows.append(
                m.VsCertificateRow(
                    tenant_id=s.tenant_id, vs_id=s.id, position=i, certificate_id=cid
                )
            )
        for i, pid in enumerate(s.policy_set_ids):
            rows.append(
                m.VsPolicySetRow(tenant_id=s.tenant_id, vs_id=s.id, position=i, policy_set_id=pid)
            )
    return rows


def rows_to_snapshot(
    *,
    tenants: Iterable[m.TenantRow],
    networks: Iterable[m.NetworkRow],
    vip_pools: Iterable[m.VipPoolRow],
    se_groups: Iterable[m.SeGroupRow],
    se_group_networks: Iterable[m.SeGroupNetworkRow],
    health_monitors: Iterable[m.HealthMonitorRow],
    certificates: Iterable[m.CertificateRow],
    policy_sets: Iterable[m.PolicySetRow],
    pools: Iterable[m.PoolRow],
    pool_members: Iterable[m.PoolMemberRow],
    virtual_services: Iterable[m.VirtualServiceRow],
    vs_ports: Iterable[m.VsPortRow],
    vs_routes: Iterable[m.VsRouteRow],
    vs_certificates: Iterable[m.VsCertificateRow],
    vs_policy_sets: Iterable[m.VsPolicySetRow],
) -> Snapshot:
    """Assemble a validated :class:`Snapshot` from ORM rows (child rows ordered by ``position``)."""
    group_networks: dict[Any, list[m.SeGroupNetworkRow]] = {}
    for gn in sorted(se_group_networks, key=lambda r: r.position):
        group_networks.setdefault(gn.se_group_id, []).append(gn)
    members: dict[Any, list[m.PoolMemberRow]] = {}
    for mem in sorted(pool_members, key=lambda r: r.position):
        members.setdefault(mem.pool_id, []).append(mem)
    ports: dict[Any, list[m.VsPortRow]] = {}
    for port in vs_ports:
        ports.setdefault(port.vs_id, []).append(port)
    routes: dict[Any, list[m.VsRouteRow]] = {}
    for route in sorted(vs_routes, key=lambda r: r.position):
        routes.setdefault(route.vs_id, []).append(route)
    certs: dict[Any, list[m.VsCertificateRow]] = {}
    for cert in sorted(vs_certificates, key=lambda r: r.position):
        certs.setdefault(cert.vs_id, []).append(cert)
    policies: dict[Any, list[m.VsPolicySetRow]] = {}
    for pol in sorted(vs_policy_sets, key=lambda r: r.position):
        policies.setdefault(pol.vs_id, []).append(pol)

    data: dict[str, Any] = {
        "tenants": [
            {
                "id": t.id,
                "version": t.version,
                "name": t.name,
                "oidc_group": t.oidc_group,
                "quotas": t.quotas,
                "require_approval": t.require_approval,
            }
            for t in tenants
        ],
        "networks": [
            {
                "id": n.id,
                "version": n.version,
                "tenant_id": n.tenant_id,
                "name": n.name,
                "cidr": _net(n.cidr),
                "gateway": _ip(n.gateway) if n.gateway is not None else None,
                "sdn_zone": n.sdn_zone,
                "vnet": n.vnet,
                "vlan_or_vni": n.vlan_or_vni,
                "managed": n.managed,
            }
            for n in networks
        ],
        "vip_pools": [
            {
                "id": vp.id,
                "version": vp.version,
                "tenant_id": vp.tenant_id,
                "name": vp.name,
                "network_id": vp.network_id,
                "ranges": vp.ranges,
            }
            for vp in vip_pools
        ],
        "se_groups": [
            {
                "id": g.id,
                "version": g.version,
                "tenant_id": g.tenant_id,
                "name": g.name,
                "ha_mode": g.ha_mode,
                "maxconn": g.maxconn,
                "canary_seconds": g.canary_seconds,
                "networks": [
                    {"network_id": gn.network_id, "role": gn.role}
                    for gn in group_networks.get(g.id, [])
                ],
            }
            for g in se_groups
        ],
        "health_monitors": [
            {
                "id": hm.id,
                "version": hm.version,
                "tenant_id": hm.tenant_id,
                "name": hm.name,
                "type": hm.type,
                "port": hm.port,
                "interval_s": hm.interval_s,
                "timeout_s": hm.timeout_s,
                "rise": hm.rise,
                "fall": hm.fall,
                "http": hm.http,
            }
            for hm in health_monitors
        ],
        "certificates": [
            {
                "id": c.id,
                "version": c.version,
                "tenant_id": c.tenant_id,
                "name": c.name,
                "source": c.source,
                "sans": c.sans,
                "not_after": c.not_after,
                "secret_ref": c.secret_ref,
                "renew_policy": c.renew_policy,
            }
            for c in certificates
        ],
        "policy_sets": [_policy_to_dict(ps) for ps in policy_sets],
        "pools": [
            {
                "id": p.id,
                "version": p.version,
                "tenant_id": p.tenant_id,
                "name": p.name,
                "algorithm": p.algorithm,
                "persistence": p.persistence,
                "health_monitor_id": p.health_monitor_id,
                "tls": p.tls,
                "tls_verify": p.tls_verify,
                "members": [
                    {
                        "id": mem.id,
                        "name": mem.name,
                        "address": _ip(mem.address),
                        "port": mem.port,
                        "weight": mem.weight,
                        "admin_state": mem.admin_state,
                    }
                    for mem in members.get(p.id, [])
                ],
            }
            for p in pools
        ],
        "virtual_services": [
            {
                "id": s.id,
                "version": s.version,
                "tenant_id": s.tenant_id,
                "name": s.name,
                "se_group_id": s.se_group_id,
                "protocol": s.protocol,
                "enabled": s.enabled,
                "vip": _ip(s.vip),
                "vip_pool_id": s.vip_pool_id,
                "default_pool_id": s.default_pool_id,
                "allow_list_id": s.allow_list_id,
                "enforce_allow_list": s.enforce_allow_list,
                "restore_client_ip_header": s.restore_client_ip_header,
                "ports": [
                    {
                        "port": p.port,
                        "tls": p.tls,
                        "http2": p.http2,
                        "proxy_protocol": p.proxy_protocol,
                        "https_redirect_port": p.https_redirect_port,
                    }
                    for p in sorted(ports.get(s.id, []), key=lambda r: r.port)
                ],
                "routes": [_route_to_dict(r) for r in routes.get(s.id, [])],
                "certificate_ids": [c.certificate_id for c in certs.get(s.id, [])],
                "policy_set_ids": [p.policy_set_id for p in policies.get(s.id, [])],
            }
            for s in virtual_services
        ],
    }
    return Snapshot.model_validate(data)


def _route_to_dict(r: m.VsRouteRow) -> dict[str, Any]:
    return {
        "order": r.route_order,
        "match": {"host": r.host, "path_prefix": r.path_prefix, "path_exact": r.path_exact},
        "pool_id": r.pool_id,
        "redirect": (
            {"location": r.redirect_location, "code": r.redirect_code}
            if r.redirect_location is not None
            else None
        ),
    }


def _policy_to_dict(ps: m.PolicySetRow) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": ps.id,
        "version": ps.version,
        "tenant_id": ps.tenant_id,
        "name": ps.name,
        "kind": ps.kind,
    }
    if ps.kind == "redirect":
        base["rules"] = ps.rules
    else:
        base["cidrs"] = [_net(c) for c in ps.cidrs or []]
        base["managed_by"] = ps.managed_by
    return base

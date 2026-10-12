"""Migration and persistence round-trip tests against a real PostgreSQL 17."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.db import migrate
from switchyard.db.base import Base
from switchyard.db.repository import (
    add_snapshot,
    find_se_group_id,
    find_tenant_id,
    load_snapshot,
)
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.domain.snapshot import Snapshot, validate_snapshot
from tests.factories import base_data


def full_data() -> dict:  # type: ignore[type-arg]
    """Base data plus every optional object kind, so the round trip touches every column."""
    data = base_data()
    data["health_monitors"] = [
        {
            "id": "hm-http",
            "tenant_id": "t-acme",
            "name": "http-200",
            "type": "http",
            "interval_s": 10,
            "http": {"path": "/healthz", "host": "app.example.test", "expect_status": [200, 204]},
        },
        {
            "id": "hm-redis",
            "tenant_id": "t-acme",
            "name": "redis",
            "type": "tcp",
            "port": 6379,
            "preset": "redis",
        },
        {
            "id": "hm-udp",
            "tenant_id": "t-acme",
            "name": "udp-probe",
            "type": "udp",
            "port": 5353,
            "udp": {"payload_hex": "00ff", "require_reply": True},
        },
    ]
    data["policy_sets"] = [
        {
            "id": "ps-redir",
            "tenant_id": "t-acme",
            "name": "dav",
            "kind": "redirect",
            "rules": [
                {
                    "match": {"path_prefix": "/.well-known/carddav"},
                    "redirect": {"location": "/remote.php/dav/", "code": 301},
                }
            ],
        },
        {
            "id": "ps-cf",
            "tenant_id": "t-acme",
            "name": "cloudflare",
            "kind": "allow_list",
            "cidrs": ["198.51.100.0/24", "2001:db8::/32"],
            "managed_by": "cloudflare",
        },
    ]
    pool = data["pools"][0]
    pool.update(
        health_monitor_id="hm-http",
        persistence={"type": "http_cookie", "name": "SY_SRV", "ttl_s": 3600, "secure": True},
        send_proxy="v2",
        backend_protocol="h2",
        tunnel_timeout_s=900,
        client_certificate_id="cert-wild",
        tls=True,
        algorithm="leastconn",
        passive_check={"observe": "layer7", "error_limit": 4, "on_error": "fastinter"},
        slow_start_s=45,
    )
    pool["members"].append(
        {
            "address": "10.20.0.12",
            "port": 80,
            "weight": 50,
            "admin_state": "drain",
            "name": "b",
            "backup": True,
            "maxconn": 200,
        }
    )
    data["pools"].append(
        {
            "id": "pool-tcp",
            "tenant_id": "t-acme",
            "name": "k8s",
            "health_monitor_id": "hm-redis",
            "members": [{"address": "10.20.0.30", "port": 6443}],
        }
    )
    data["se_groups"][0]["peers"] = {
        "port": 10001,
        "members": [
            {"name": "se-1", "address": "10.20.0.5"},
            {"name": "se-2", "address": "10.20.0.6"},
        ],
    }
    data["certificates"][0].update(source="acme", ocsp_stapling=True, renew_before_days=14)
    vs = data["virtual_services"][0]
    vs["ports"][0]["tls_profile"] = "modern"
    vs["ports"][0]["http3"] = True
    vs["compression"] = {"algorithms": ["gzip", "deflate"], "content_types": ["text/html"]}
    vs.update(
        policy_set_ids=["ps-redir"],
        allow_list_id="ps-cf",
        enforce_allow_list=True,
        restore_client_ip_header="CF-Connecting-IP",
    )
    vs["routes"] += [
        {
            "order": 1,
            "match": {"host": "app.example.test", "path_prefix": "/old"},
            "redirect": {"location": "https://app.example.test/new", "code": 302},
        },
        {"order": 2, "match": {"path_exact": "/status"}, "pool_id": "pool-web"},
    ]
    data["virtual_services"].append(
        {
            "id": "vs-k8s",
            "tenant_id": "t-acme",
            "se_group_id": "seg-1",
            "name": "k8s-api",
            "protocol": "tcp",
            "enabled": False,
            "vip": "192.0.2.21",
            "ports": [{"port": 6443, "proxy_protocol": True}, {"port": 9345}],
            "default_pool_id": "pool-tcp",
        }
    )
    data["networks"][0].update(sdn_zone="zone1", vnet="vnetlb", vlan_or_vni=120, managed=True)
    data["tenants"][0].update(quotas={"max_vs": 50})
    return data


async def test_round_trip_preserves_everything(admin_engine: AsyncEngine) -> None:
    original = snapshot_from_mapping(full_data())
    assert validate_snapshot(original) == []
    async with admin_session(admin_engine) as s:
        await add_snapshot(s, original)
    async with admin_session(admin_engine) as s:
        loaded = await load_snapshot(s)

    def by_name(snap: Snapshot) -> dict:  # type: ignore[type-arg]
        out = snap.model_dump(mode="json")
        for key, value in out.items():
            out[key] = sorted(value, key=lambda o: o["name"])
        for vs in out["virtual_services"]:
            vs["ports"].sort(key=lambda p: p["port"])  # port order is not significant
        return out

    assert by_name(loaded) == by_name(original)
    assert validate_snapshot(loaded) == []


async def test_lookup_helpers_and_se_group_slice(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    async with admin_session(admin_engine) as s:
        await add_snapshot(s, snapshot_from_mapping(full_data()))
        tenant = await find_tenant_id(s, "acme")
        assert tenant is not None
        assert await find_tenant_id(s, "nope") is None
    async with tenant_session(app_engine, [tenant]) as s:
        group = await find_se_group_id(s, tenant, "edge")
        assert group is not None
        assert await find_se_group_id(s, tenant, "nope") is None
        sliced = (await load_snapshot(s)).for_se_group(group)
    assert validate_snapshot(sliced) == []
    assert {v.name for v in sliced.virtual_services} == {"web", "k8s-api"}


async def test_member_and_port_order_is_stable(admin_engine: AsyncEngine) -> None:
    async with admin_session(admin_engine) as s:
        await add_snapshot(s, snapshot_from_mapping(full_data()))
    async with admin_session(admin_engine) as s:
        loaded = await load_snapshot(s)
    pool = next(p for p in loaded.pools if p.name == "web")
    assert [m.server_name for m in pool.members] == ["10-20-0-11-8080", "b"]
    web = next(v for v in loaded.virtual_services if v.name == "web")
    assert [r.order for r in web.routes] == [0, 1, 2]
    assert [str(c) for c in web.certificate_ids] == [symbolic_uuid("cert-wild")]


def test_models_and_migrations_agree(pg_url: str) -> None:
    engine = create_engine(pg_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def test_downgrade_and_upgrade_round_trip(pg_url: str) -> None:
    migrate.downgrade(pg_url, "base")
    migrate.upgrade(pg_url)

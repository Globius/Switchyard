"""virtual_service_states: UDP is served by IPVS since M6."""

from datetime import UTC, datetime
from uuid import UUID

from switchyard.services.seed import example_snapshot
from switchyard.services.status import (
    EngineStatus,
    VirtualServiceStatus,
    virtual_service_states,
)


def test_udp_and_http_are_serving_and_disabled_is_reported() -> None:
    example = example_snapshot()
    vs = example.virtual_services[0]
    udp = vs.model_copy(update={"name": "zz-dns", "protocol": "udp", "ports": [], "routes": []})
    off = vs.model_copy(update={"name": "zz-off", "enabled": False})
    snap = example.model_copy(update={"virtual_services": [vs, udp, off]})
    states = {v.name: v.state for v in virtual_service_states(snap)}
    assert states["zz-dns"] == "serving"
    assert states["zz-off"] == "disabled"
    assert states[vs.name] == "serving"


def _engine(group: UUID, members: list[dict[str, str]], reachable: bool = True) -> EngineStatus:
    import uuid

    return EngineStatus(
        engine_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        se_group_id=group,
        name="e",
        reachable=reachable,
        error=None,
        haproxy_fingerprint=None,
        members=members,
        agent=None,
        drift=[],
        observed_at=datetime.now(UTC),
    )


def test_health_follows_member_check_results() -> None:
    from switchyard.render.escape import ident

    snap = example_snapshot()
    vs = next(v for v in snap.virtual_services if v.name == "k8s-api")
    pool = next(p for p in snap.pools if p.id == vs.default_pool_id)
    backend = "pool_" + ident(pool.name)

    def rows(*statuses: str) -> list[dict[str, str]]:
        return [
            {"backend": backend, "server": f"s{i}", "status": s} for i, s in enumerate(statuses)
        ]

    def health(*engines: EngineStatus) -> VirtualServiceStatus:
        return {v.name: v for v in virtual_service_states(snap, engines)}[vs.name]

    g = vs.se_group_id
    assert health().health == "unknown"
    assert health(_engine(g, rows("UP", "UP"))).health == "healthy"
    assert health(_engine(g, rows("UP", "DOWN"))).health == "degraded"
    assert health(_engine(g, rows("DRAIN", "DOWN"))).health == "down"
    assert health(_engine(g, rows("UP")), _engine(g, [])).health == "degraded"
    assert health(_engine(g, rows("UP"), reachable=False)).health == "unknown"
    got = health(_engine(g, rows("UP 1/2", "DOWN")))
    assert (got.members_up, got.members_total) == (1, 2)
    off = snap.model_copy(update={"virtual_services": [vs.model_copy(update={"enabled": False})]})
    assert virtual_service_states(off)[0].health == "disabled"

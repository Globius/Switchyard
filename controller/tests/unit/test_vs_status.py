"""virtual_service_states: UDP is served by IPVS since M6."""

from switchyard.services.seed import example_snapshot
from switchyard.services.status import virtual_service_states


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

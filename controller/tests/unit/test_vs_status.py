"""virtual_service_states: UDP reports ``not_served``, never ``down``."""

from switchyard.services.seed import example_snapshot
from switchyard.services.status import virtual_service_states


def test_udp_is_not_served_and_http_is_serving() -> None:
    example = example_snapshot()
    vs = example.virtual_services[0]
    udp = vs.model_copy(update={"name": "zz-dns", "protocol": "udp", "ports": [], "routes": []})
    off = vs.model_copy(update={"name": "zz-off", "enabled": False})
    snap = example.model_copy(update={"virtual_services": [vs, udp, off]})
    states = {v.name: v.state for v in virtual_service_states(snap)}
    assert states["zz-dns"] == "not_served"
    assert states["zz-off"] == "disabled"
    assert states[vs.name] == "serving"

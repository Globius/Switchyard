"""The signature vector shared with the device agent (``posture-agent``) must still verify."""

import base64
import json
from pathlib import Path

from switchyard.services.devices import canonical, parse_public_key

VECTOR = (
    Path(__file__).parents[3]
    / "posture-agent"
    / "internal"
    / "protocol"
    / "testdata"
    / "vector.json"
)


def test_the_agents_signature_verifies_against_the_controllers_canonical_form() -> None:
    v = json.loads(VECTOR.read_text("utf-8"))
    body = v["body"].encode()
    data = canonical(v["method"], v["path"], v["timestamp_ms"], body)
    assert data.decode() == v["canonical"]
    parse_public_key(v["public_key_b64"]).verify(base64.b64decode(v["signature_b64"]), data)

"""RADIUS client: packet codec, authenticity checks and a full exchange against a fake server."""

import asyncio

import pytest

from switchyard.adapters import radius as r

SECRET = b"shared-secret-1"


def _req(state: bytes = b"") -> bytes:
    return r.build_access_request(
        SECRET,
        9,
        "alice",
        "pw",
        nas_identifier="sy",
        state=state,
        authenticator=bytes(range(16)),
    )


def test_request_roundtrip_and_message_authenticator() -> None:
    packet = r.parse_packet(_req())
    assert (packet.code, packet.identifier) == (r.ACCESS_REQUEST, 9)
    assert packet.first(r.USER_NAME) == b"alice"
    assert packet.first(r.USER_PASSWORD) != b"pw"
    mac = packet.first(r.MESSAGE_AUTHENTICATOR)
    assert mac is not None
    zeroed = r._zero_message_authenticator(packet.raw, packet.authenticator)
    assert r._message_authenticator(zeroed, SECRET) == mac


def test_long_password_is_chained() -> None:
    blob = r.encrypt_password(b"x" * 40, SECRET, bytes(16))
    assert len(blob) == 48
    with pytest.raises(r.RadiusError):
        r.encrypt_password(b"", SECRET, bytes(16))
    with pytest.raises(r.RadiusError):
        r.encrypt_password(b"x" * 129, SECRET, bytes(16))


def test_response_must_be_authentic() -> None:
    req = _req()
    good = r.build_response(r.ACCESS_ACCEPT, req, SECRET, [(r.FILTER_ID, b"staff")])
    assert r.interpret(r.verify_response(good, req, SECRET)).groups == ("staff",)
    with pytest.raises(r.RadiusError, match="authentic"):
        r.verify_response(good, req, b"another-secret")
    tampered = bytearray(good)
    tampered[-1] ^= 1
    with pytest.raises(r.RadiusError):
        r.verify_response(bytes(tampered), req, SECRET)
    other = r.build_access_request(SECRET, 10, "a", "p", nas_identifier="n")
    with pytest.raises(r.RadiusError, match="identifier"):
        r.verify_response(good, other, SECRET)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x01" * 19,
        b"\x02\x01\x00\x30" + bytes(16),
        b"\x02\x01\x00\x16" + bytes(16) + b"\x01\x01",
    ],
)
def test_malformed_packets_are_rejected(data: bytes) -> None:
    with pytest.raises(r.RadiusError):
        r.parse_packet(data)


def test_attribute_limits() -> None:
    with pytest.raises(r.RadiusError):
        r.build_access_request(SECRET, 1, "", "p", nas_identifier="n")
    with pytest.raises(r.RadiusError):
        r.build_access_request(SECRET, 1, "u" * 254, "p", nas_identifier="n")


def test_interpret_challenge_and_reject_strip_control_characters() -> None:
    req = _req()
    ans = r.build_response(
        r.ACCESS_CHALLENGE, req, SECRET, [(r.REPLY_MESSAGE, b"Code:\x1b[31m"), (r.STATE, b"abc")]
    )
    res = r.interpret(r.verify_response(ans, req, SECRET))
    assert (res.status, res.state) == ("challenge", b"abc")
    assert "\x1b" not in res.message
    rej = r.build_response(r.ACCESS_REJECT, req, SECRET)
    assert r.interpret(r.verify_response(rej, req, SECRET)).status == "reject"


def test_class_attribute_as_group_source() -> None:
    req = _req()
    ans = r.build_response(r.ACCESS_ACCEPT, req, SECRET, [(r.CLASS, b"ops"), (r.CLASS, b"ops")])
    assert r.interpret(r.verify_response(ans, req, SECRET), "class").groups == ("ops",)


def _client(port: int, secret: bytes = SECRET, **kw: object) -> r.UdpRadiusClient:
    return r.UdpRadiusClient(
        r.RadiusServer("127.0.0.1", port, secret, timeout_s=0.3, retries=1, allow_local=True, **kw)  # type: ignore[arg-type]
    )


def test_exchange_against_fake_server() -> None:
    srv = r.FakeRadiusServer(SECRET, {"alice": ("pw", ("staff", "ops")), "bob": ("b", ())})
    port = srv.start()
    try:
        c = _client(port)
        ok = asyncio.run(c.authenticate("alice", "pw"))
        assert (ok.status, ok.groups) == ("accept", ("staff", "ops"))
        assert asyncio.run(c.authenticate("alice", "nope")).status == "reject"
        assert asyncio.run(c.authenticate("nobody", "pw")).status == "reject"
        assert srv.requests[0].first(r.NAS_IDENTIFIER) == b"switchyard"
    finally:
        srv.stop()


def test_challenge_round() -> None:
    srv = r.FakeRadiusServer(SECRET, {"alice": ("pw", ("staff",))}, challenge={"alice": "123456"})
    port = srv.start()
    try:
        c = _client(port)
        first = asyncio.run(c.authenticate("alice", "pw"))
        assert first.status == "challenge" and first.state
        assert asyncio.run(c.authenticate("alice", "000000", state=first.state)).status == "reject"
        done = asyncio.run(c.authenticate("alice", "123456", state=first.state))
        assert (done.status, done.groups) == ("accept", ("staff",))
    finally:
        srv.stop()


def test_wrong_secret_and_silence_are_errors_not_success() -> None:
    srv = r.FakeRadiusServer(SECRET, {"alice": ("pw", ())})
    port = srv.start()
    try:
        with pytest.raises(r.RadiusError):
            asyncio.run(_client(port, b"wrong-secret-2").authenticate("alice", "pw"))
    finally:
        srv.stop()
    with pytest.raises(r.RadiusError):
        asyncio.run(_client(port).authenticate("alice", "pw"))  # nobody listens any more


def test_a_response_without_message_authenticator_is_refused_unless_allowed() -> None:
    req = _req()
    bare = r.build_response(r.ACCESS_ACCEPT, req, SECRET, message_authenticator=False)
    with pytest.raises(r.RadiusError, match="Message-Authenticator"):
        r.verify_response(bare, req, SECRET)
    assert r.verify_response(bare, req, SECRET, require_message_authenticator=False).code == 2


def test_a_forged_message_authenticator_is_refused() -> None:
    req = _req()
    good = bytearray(r.build_response(r.ACCESS_ACCEPT, req, SECRET))
    # attribute 80 comes first: flip a bit of its value, then fix the Response Authenticator so only
    # the HMAC can catch it
    good[22] ^= 1
    import hashlib

    good[4:20] = hashlib.md5(  # noqa: S324 - the protocol's own construction
        bytes(good[:4]) + req[4:20] + bytes(good[20:]) + SECRET
    ).digest()
    with pytest.raises(r.RadiusError, match="Message-Authenticator"):
        r.verify_response(bytes(good), req, SECRET)


def test_a_legacy_server_needs_to_be_allowed_explicitly() -> None:
    srv = r.FakeRadiusServer(SECRET, {"alice": ("pw", ())}, sign_responses=False)
    port = srv.start()
    try:
        strict = r.UdpRadiusClient(
            r.RadiusServer("127.0.0.1", port, SECRET, timeout_s=0.3, retries=0, allow_local=True)
        )
        with pytest.raises(r.RadiusError):
            asyncio.run(strict.authenticate("alice", "pw"))
        lenient = r.UdpRadiusClient(
            r.RadiusServer(
                "127.0.0.1",
                port,
                SECRET,
                timeout_s=0.3,
                retries=0,
                allow_local=True,
                require_message_authenticator=False,
            )
        )
        assert asyncio.run(lenient.authenticate("alice", "pw")).status == "accept"
    finally:
        srv.stop()


def test_the_client_refuses_a_name_that_resolves_to_a_local_address() -> None:
    client = r.UdpRadiusClient(r.RadiusServer("localhost", 1812, SECRET, timeout_s=0.2, retries=0))
    with pytest.raises(r.RadiusError, match="will not contact"):
        asyncio.run(client.authenticate("alice", "pw"))

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The login gate: flow engine, cookies and the HTTP behaviour against a fake RADIUS and fake Dex."""

import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

from switchyard.adapters.radius import FakeRadiusServer
from switchyard.gate import app as gate_app
from switchyard.gate import flow as fl

SECRET = b"radius-shared-1"
SIGNING = "signing-secret-0123456789abcdef"
PATH = "/dex/acme/staff"
CALLBACK = f"{PATH}/callback?state=abc"


def config(
    main_port: int, otp_port: int, steps: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "name": "acme-staff",
        "path": PATH,
        "dex_port": 5600,
        "servers": {
            "main": {
                "host": "127.0.0.1",
                "port": main_port,
                "secret": SECRET.decode(),
                "timeout_s": 1,
                "retries": 0,
                "allow_local": True,
            },
            "otp": {
                "host": "127.0.0.1",
                "port": otp_port,
                "secret": SECRET.decode(),
                "timeout_s": 1,
                "retries": 0,
                "allow_local": True,
            },
        },
        "steps": steps
        or [
            {"name": "password", "kind": "password", "server": "main", "prompt": "Password"},
            {
                "name": "otp",
                "kind": "code",
                "server": "otp",
                "prompt": "One-time code",
                "when": {"groups_any": ["admins"]},
            },
        ],
        "email_domain": "example.test",
        "signing_secret": SIGNING,
        "hosts": {"app.example.test": ["/oauth2"]},
    }


class Env:
    def __init__(self, tmp: Path, steps: list[dict[str, Any]] | None = None) -> None:
        self.main = FakeRadiusServer(
            SECRET,
            {
                "alice": ("pw", ("staff", "sy-factors-9")),
                "root": ("pw", ("staff", "admins")),
                "carol": ("pw", ("staff",)),
            },
            challenge={"carol": "246810"},
        )
        self.otp = FakeRadiusServer(SECRET, {"root": ("123456", ()), "alice": ("123456", ())})
        main, otp = self.main.start(), self.otp.start()
        (tmp / "gate").mkdir()
        (tmp / "gate" / "acme-staff.json").write_text(json.dumps(config(main, otp, steps)))
        self.seen: list[httpx.Request] = []
        self.dex = httpx.AsyncClient(transport=httpx.MockTransport(self._dex))
        gate = gate_app.Gate(gate_app.ConfigSet(tmp / "gate", recheck_s=0), http=self.dex)
        self.client = TestClient(
            gate.app(), base_url="https://auth.example.test", follow_redirects=False
        )

    def _dex(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(
            302, headers={"Location": "https://app.example.test/oauth2/callback?code=1"}
        )

    def csrf(self, url: str = f"{PATH}/_gate/login") -> str:
        page = self.client.get(url).text
        return re.search(r'name=csrf value="([^"]+)"', page).group(1)  # type: ignore[union-attr]

    def login(self, user: str, secret: str, target: str = CALLBACK) -> Any:
        url = f"{PATH}/_gate/login?return=" + httpx.QueryParams({"x": target})["x"].replace(
            "?", "%3F"
        ).replace("=", "%3D")
        return self.client.post(
            url, data={"username": user, "secret": secret, "csrf": self.csrf(url)}
        )

    def step(self, secret: str) -> Any:
        url = f"{PATH}/_gate/login"
        return self.client.post(url, data={"secret": secret, "csrf": self.csrf(url)})

    def close(self) -> None:
        self.main.stop()
        self.otp.stop()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    try:
        yield e
    finally:
        e.close()


def remote(e: Env) -> httpx.Request:
    return e.seen[-1]


# ----------------------------------------------------------------------------- engine


def cfg_for(steps: list[dict[str, Any]]) -> fl.GateConfig:
    return fl.GateConfig.from_mapping(config(1, 2, steps))


def test_conditions_by_group_and_address() -> None:
    cfg = cfg_for(
        [
            {"name": "a"},
            {
                "name": "b",
                "kind": "code",
                "server": "otp",
                "when": {"source_not_in": ["10.0.0.0/8"]},
            },
            {"name": "c", "kind": "code", "server": "otp", "when": {"source_in": ["192.0.2.0/24"]}},
        ]
    )
    assert fl.next_step(cfg, [0], [], "10.1.1.1") is None
    assert fl.next_step(cfg, [0], [], "203.0.113.5") == 1
    assert fl.next_step(cfg, [0, 1], [], "192.0.2.9") == 2
    assert fl.next_step(cfg, [0, 1], [], "203.0.113.5") is None
    # an unreadable address never skips a factor that is asked of "everyone outside"
    assert fl.next_step(cfg, [0], [], None) == 1
    assert fl.next_step(cfg, [0], [], "not-an-ip") == 1


def test_want_ignores_conditions_and_counts_factors() -> None:
    cfg = cfg_for([{"name": "a"}, {"name": "b", "kind": "code", "when": {"groups_any": ["x"]}}])
    assert fl.next_step(cfg, [0], [], None, want=2) == 1
    assert fl.next_step(cfg, [0, 1], [], None, want=2) is None


def test_tokens_expire_and_cannot_cross_kinds() -> None:
    t = fl.sign(SIGNING, "session", {"u": "a"}, 60, now=1000)
    assert fl.verify(SIGNING, "session", t, now=1059) is not None
    assert fl.verify(SIGNING, "session", t, now=1061) is None
    assert fl.verify(SIGNING, "flow", t, now=1010) is None
    assert fl.verify("another-secret-0123456789", "session", t, now=1010) is None
    body, mac = t.split(".")
    assert fl.verify(SIGNING, "session", f"{body}x.{mac}", now=1010) is None
    assert fl.verify(SIGNING, "session", "garbage", now=1010) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.pop("steps"),
        lambda c: c.update(steps=[]),
        lambda c: c.update(signing_secret="short"),
        lambda c: c["steps"][0].update(server="nope"),
        lambda c: c.update(servers={}),
    ],
)
def test_bad_configuration_is_refused(mutate: Any) -> None:
    data = config(1, 2)
    mutate(data)
    with pytest.raises(ValueError):
        fl.GateConfig.from_mapping(data)


def test_user_names_and_emails() -> None:
    cfg = cfg_for([{"name": "a"}])
    assert fl.valid_user("a.b-c_d@corp") and not fl.valid_user("a b") and not fl.valid_user("a\nb")
    assert fl.email_of(cfg, "alice") == "alice@example.test"
    assert fl.email_of(cfg, "a@corp.test") == "a@corp.test"


def test_throttle_locks_a_pair_and_then_the_address() -> None:
    t = gate_app.Throttle(limit=2, window_s=10)
    t.failure("A", "1.1.1.1", now=0)
    t.failure("a", "1.1.1.1", now=1)
    assert t.locked("a", "1.1.1.1", now=2)
    assert not t.locked("b", "1.1.1.1", now=2)
    assert not t.locked("a", "1.1.1.1", now=20)


# ------------------------------------------------------------------------------ gate


def test_callback_without_a_sign_in_goes_to_the_form(env: Env) -> None:
    r = env.client.get(CALLBACK)
    assert r.status_code == 302
    assert r.headers["location"].startswith(f"{PATH}/_gate/login?return=")
    assert not env.seen  # Dex never saw an unauthenticated callback


def test_sign_in_then_callback_carries_identity_to_dex(env: Env) -> None:
    r = env.login("alice", "pw")
    assert r.status_code == 303 and r.headers["location"] == CALLBACK
    assert "sy_gate" in r.headers["set-cookie"] and "Secure" in r.headers["set-cookie"]
    env.client.get(CALLBACK, headers={"X-Remote-User": "root", "X-Remote-Group": "admins"})
    req = remote(env)
    assert req.headers["x-remote-user"] == "alice"
    assert req.headers["x-remote-user-email"] == "alice@example.test"
    # the group a server returns with the reserved prefix is dropped; the factor count is the gate's
    assert req.headers["x-remote-group"] == "staff,sy-factors-1"


def test_wrong_password_is_a_generic_failure_without_a_session(env: Env) -> None:
    r = env.login("alice", "nope")
    assert r.status_code == 401 and "Sign-in failed." in r.text
    assert "sy_gate" not in r.headers.get("set-cookie", "")
    assert env.login("nobody", "pw").text.count("Sign-in failed.") == 1


def test_five_failures_lock_the_user(env: Env) -> None:
    for _ in range(5):
        env.login("alice", "nope")
    r = env.login("alice", "pw")
    assert r.status_code == 429


def test_radius_down_is_unavailable_not_signed_in(env: Env) -> None:
    env.main.stop()
    r = env.login("alice", "pw")
    assert r.status_code == 503 and "sy_gate" not in r.headers.get("set-cookie", "")


def test_form_posts_need_the_csrf_value(env: Env) -> None:
    env.csrf()
    r = env.client.post(
        f"{PATH}/_gate/login", data={"username": "alice", "secret": "pw", "csrf": "x"}
    )
    assert r.status_code == 400


def test_return_target_cannot_leave_the_directory(env: Env) -> None:
    for bad in ("https://evil.test/", "//evil.test/x", "/elsewhere", f"{PATH}//evil.test"):
        url = f"{PATH}/_gate/login?return={bad}"
        r = env.client.post(url, data={"username": "alice", "secret": "pw", "csrf": env.csrf(url)})
        assert r.headers["location"] == f"{PATH}/", bad


def test_a_second_step_is_asked_only_when_the_condition_holds(env: Env) -> None:
    r = env.login("root", "pw")  # root is in admins
    assert r.status_code == 200 and "One-time code" in r.text and 'name="username"' not in r.text
    assert env.step("000000").status_code == 401
    done = env.step("123456")
    assert done.status_code == 303
    env.client.get(CALLBACK)
    assert remote(env).headers["x-remote-group"] == "admins,staff,sy-factors-1,sy-factors-2"


def test_a_challenge_is_answered_on_the_same_step(env: Env) -> None:
    r = env.login("carol", "pw")
    assert r.status_code == 200 and "Enter your one-time code" in r.text
    assert env.step("111111").status_code == 401
    r2 = env.login("carol", "pw")
    assert "Enter your one-time code" in r2.text
    assert env.step("246810").status_code == 303


def test_other_paths_go_to_dex_without_client_supplied_identity(env: Env) -> None:
    r = env.client.get(
        f"{PATH}/.well-known/openid-configuration", headers={"X-Remote-User": "root"}
    )
    assert r.status_code == 302
    assert "x-remote-user" not in remote(env).headers
    assert env.client.get("/other").status_code == 404
    assert env.client.get(f"{PATH}/_gate/nothing").status_code == 404


def test_dex_down_is_a_bad_gateway(tmp_path: Path) -> None:
    e = Env(tmp_path)
    try:

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        gate = gate_app.Gate(
            gate_app.ConfigSet(tmp_path / "gate", recheck_s=0),
            http=httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        )
        client = TestClient(gate.app(), base_url="https://auth.example.test")
        assert client.get(f"{PATH}/keys").status_code == 502
    finally:
        e.close()


# ------------------------------------------------------------------------------ step-up


def elevate_url(level: int = 2, host: str = "app.example.test", path: str = "/admin") -> str:
    return f"{PATH}/_gate/elevate?level={level}&host={host}&path={path}"


def test_step_up_asks_the_missing_factor_and_raises_the_count(env: Env) -> None:
    assert env.login("alice", "pw").status_code == 303  # staff: one factor
    r = env.client.get(elevate_url())
    assert r.status_code == 303 and r.headers["location"] == f"{PATH}/_gate/login"
    page = env.client.get(r.headers["location"])
    assert "One-time code" in page.text
    assert env.step("000000").status_code == 401
    done = env.step("123456")
    assert done.status_code == 303
    assert done.headers["location"] == "https://app.example.test/oauth2/start?rd=%2Fadmin"
    env.client.get(CALLBACK)
    assert remote(env).headers["x-remote-group"] == "staff,sy-factors-1,sy-factors-2"
    # already at the level: straight back
    again = env.client.get(elevate_url())
    assert again.headers["location"].startswith("https://app.example.test/oauth2/start")


def test_step_up_without_a_session_signs_in_first(env: Env) -> None:
    r = env.client.get(elevate_url())
    assert r.status_code == 302 and "_gate/login?return=" in r.headers["location"]
    target = parse_qs(urlsplit(r.headers["location"]).query)["return"][0]
    assert target.startswith(f"{PATH}/_gate/elevate?")


@pytest.mark.parametrize(
    "url",
    [
        elevate_url(host="evil.test"),
        elevate_url(path="//evil.test"),
        elevate_url(path="/a b"),
        elevate_url(level=1),
        elevate_url(level=3),
        f"{PATH}/_gate/elevate?level=x&host=app.example.test&path=/",
    ],
)
def test_step_up_only_returns_to_the_directorys_own_services(env: Env, url: str) -> None:
    env.login("alice", "pw")
    assert env.client.get(url).status_code == 400


def test_a_signed_session_from_another_directory_is_not_accepted(env: Env) -> None:
    forged = fl.sign(
        "different-secret-0123456789abc", "session", {"u": "root", "g": [], "d": [0]}, 600
    )
    env.client.cookies.set("sy_gate", forged, path=f"{PATH}/")
    assert env.client.get(CALLBACK).status_code == 302
    assert not env.seen


def test_configuration_changes_are_picked_up(tmp_path: Path) -> None:
    (tmp_path / "gate").mkdir()
    cs = gate_app.ConfigSet(tmp_path / "gate", recheck_s=0)
    assert cs.all() == [] and cs.find(PATH + "/x") is None
    (tmp_path / "gate" / "bad.json").write_text("{not json")
    assert cs.all() == []
    (tmp_path / "gate" / "ok.json").write_text(json.dumps(config(1, 2)))
    time.sleep(0.01)
    found = cs.find(PATH + "/callback")
    assert found is not None and found[1] == "/callback"
    assert cs.find(PATH + "x/callback") is None


def test_dex_connector_callback_path_is_guarded_too(env: Env) -> None:
    """Dex calls its connector back on ``/callback/<connector id>``; that path needs a sign-in."""
    assert env.client.get(f"{PATH}/callback/radius?state=abc").status_code == 302
    assert not env.seen
    env.login("alice", "pw", f"{PATH}/callback/radius?state=abc")
    env.client.get(f"{PATH}/callback/radius?state=abc")
    assert remote(env).headers["x-remote-user"] == "alice"


def test_step_up_may_return_to_a_path_with_a_query(env: Env) -> None:
    env.login("alice", "pw")
    r = env.client.get(f"{PATH}/_gate/elevate?level=2&host=app.example.test&path=/admin%3Fa%3D1")
    assert r.status_code == 303 and r.headers["location"] == f"{PATH}/_gate/login"
    done = env.step("123456")
    assert done.headers["location"].endswith("start?rd=%2Fadmin%3Fa%3D1")

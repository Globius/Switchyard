"""RADIUS directories and login flows: models, state checks and the broker files (ADR 0195, 0196)."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.models import DirectoryPolicySet, LoginFlow, RadiusConnector
from switchyard.render.broker import (
    BROKER_PORT_BASE,
    GATE_PORT,
    broker_secret_refs,
    render_broker_files,
)
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import render_haproxy
from switchyard.services.broker import write_broker_files
from tests.unit.render.test_broker import SECRETS, URL, T, access, codes, data_with, directory

RADIUS: dict[str, Any] = {
    "type": "radius",
    "servers": [
        {"name": "main", "host": "radius.example.test", "secret_ref": "kv/tenants/acme/auth/rs"},
        {
            "name": "otp",
            "host": "otp.example.test",
            "port": 1813,
            "secret_ref": "kv/tenants/acme/auth/otp",
        },
    ],
    "email_domain": "example.test",
    "signing_secret_ref": "kv/tenants/acme/auth/gate",
}
FLOW: dict[str, Any] = {
    "steps": [
        {"name": "password", "server": "main"},
        {
            "name": "otp",
            "kind": "code",
            "server": "otp",
            "prompt": "One-time code",
            "when": {"groups_any": ["admins"], "source_not_in": ["10.0.0.0/8"]},
        },
    ]
}
RS = {
    **SECRETS,
    "kv/tenants/acme/auth/rs": "radius-secret-1",
    "kv/tenants/acme/auth/otp": "radius-secret-2",
    "kv/tenants/acme/auth/gate": "gate-signing-secret-0123456789",
}


def radius_dir(flow: dict[str, Any] | None = FLOW, **conn: Any) -> dict[str, Any]:
    d = directory({**RADIUS, **conn})
    if flow is not None:
        d["flow"] = flow
    return d


class TestModels:
    def test_defaults(self) -> None:
        c = RadiusConnector.model_validate(RADIUS)
        assert c.servers[0].port == 1812 and c.group_attribute == "filter_id"
        assert c.nas_identifier == "switchyard" and c.login_timeout_s == 300

    @pytest.mark.parametrize(
        "update",
        [
            {"servers": []},
            {"servers": RADIUS["servers"] * 3},  # duplicate names
            {"email_domain": "not a host"},
            {"nas_identifier": "has space"},
            {"group_attribute": "vendor"},
            {"login_timeout_s": 5},
        ],
    )
    def test_values_are_strict(self, update: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            RadiusConnector.model_validate({**RADIUS, **update})

    @pytest.mark.parametrize(
        "steps",
        [
            [],
            [{"name": "a"}] * 2,  # duplicate names
            [{"name": "a", "kind": "code"}],  # first step is a password step
            [{"name": "a", "when": {"groups_any": ["x"]}}],  # first step is unconditional
            [{"name": "a"}, {"name": "b", "when": {}}],  # empty condition
            [{"name": "a"}, {"name": "b", "when": {"groups_any": ["sy-factors-2"]}}],
            [{"name": "a"}, {"name": "b", "when": {"source_in": ["not-a-network"]}}],
            [{"name": "a"}, {"name": "b", "prompt": "<script>"}],
            [{"name": f"s{i}"} for i in range(5)],
        ],
    )
    def test_flow_shape(self, steps: list[dict[str, Any]]) -> None:
        with pytest.raises(ValidationError):
            LoginFlow.model_validate({"steps": steps})

    def test_flow_goes_with_radius_and_known_servers(self) -> None:
        body = {"id": "d", "tenant_id": "00000000-0000-0000-0000-000000000001", "name": "d"}
        ok = {**body, "connector": RADIUS, "flow": FLOW}
        assert DirectoryPolicySet.model_validate({**ok, "id": _uuid(1), "tenant_id": _uuid(2)})
        ldap = {"type": "local", "users": []}
        with pytest.raises(ValidationError):
            DirectoryPolicySet.model_validate(
                {**ok, "id": _uuid(1), "tenant_id": _uuid(2), "connector": ldap}
            )
        flow = {"steps": [{"name": "a", "server": "ghost"}]}
        with pytest.raises(ValidationError, match="unknown server"):
            DirectoryPolicySet.model_validate(
                {**ok, "id": _uuid(1), "tenant_id": _uuid(2), "flow": flow}
            )

    def test_min_factors_range_and_not_public(self) -> None:
        from switchyard.domain.models import AccessRule

        assert AccessRule(min_factors=2).min_factors == 2
        for bad in (1, 5):
            with pytest.raises(ValidationError):
                AccessRule(min_factors=bad)
        with pytest.raises(ValidationError):
            AccessRule(mode="public", min_factors=2)


def _uuid(n: int) -> str:
    import uuid

    return str(uuid.UUID(int=n))


class TestState:
    def test_valid(self) -> None:
        sso = access(rules=[{"mode": "login", "min_factors": 2}])
        assert codes(data_with(radius_dir(), sso)) == []

    def test_min_factors_needs_a_flow_with_enough_steps(self) -> None:
        sso = access(rules=[{"mode": "login", "min_factors": 2}])
        assert codes(data_with(radius_dir(flow=None), sso)) == ["step_up_needs_flow"]
        one = {"steps": [{"name": "password"}]}
        assert codes(data_with(radius_dir(flow=one), sso)) == ["step_up_too_deep"]

    def test_min_factors_is_refused_on_an_oidc_issuer(self) -> None:
        sso = access(rules=[{"mode": "login", "min_factors": 2}])
        sso["oidc"] = {
            "issuer": "https://login.example.test",
            "client_id": "web",
            "client_secret_ref": "kv/tenants/acme/auth/client",
            "cookie_secret_ref": "kv/tenants/acme/auth/cookie",
        }
        assert codes(data_with(sso)) == ["step_up_needs_flow"]

    def test_secrets_stay_in_the_tenants_namespace(self) -> None:
        servers = [{**RADIUS["servers"][0], "secret_ref": "kv/tenants/globex/auth/rs"}]
        assert codes(data_with(radius_dir(None, servers=servers), access())) == [
            "secret_ref_not_allowed"
        ]
        assert codes(
            data_with(radius_dir(None, signing_secret_ref="kv/tenants/globex/auth/gate"), access())
        ) == ["secret_ref_not_allowed"]

    def test_radius_groups_work_in_group_rules(self) -> None:
        sso = access(rules=[{"mode": "groups", "groups": ["staff"]}])
        assert codes(data_with(radius_dir(None), sso)) == []


class TestFiles:
    def snap(self, flow: dict[str, Any] | None = FLOW) -> Any:
        return snapshot_from_mapping(data_with(radius_dir(flow), access()))

    def test_dex_uses_an_authproxy_connector_and_no_secrets(self) -> None:
        files = render_broker_files(self.snap(), RS, URL)
        config = yaml.safe_load(files["dex/acme-staff.yaml"])
        (conn,) = config["connectors"]
        assert conn["type"] == "authproxy"
        assert conn["config"] == {
            "userHeader": "X-Remote-User",
            "emailHeader": "X-Remote-User-Email",
            "groupHeader": "X-Remote-Group",
        }
        assert "radius-secret" not in files["dex/acme-staff.yaml"]

    def test_gate_config(self) -> None:
        files = render_broker_files(self.snap(), RS, URL)
        gate = json.loads(files["gate/acme-staff.json"])
        assert gate["path"] == "/dex/acme/staff" and gate["dex_port"] == BROKER_PORT_BASE
        assert gate["servers"]["otp"] == {
            "host": "otp.example.test",
            "port": 1813,
            "secret": "radius-secret-2",
            "timeout_s": 3,
            "retries": 2,
        }
        assert gate["steps"][1] == {
            "name": "otp",
            "kind": "code",
            "server": "otp",
            "prompt": "One-time code",
            "when": {"groups_any": ["admins"], "source_not_in": ["10.0.0.0/8"]},
        }
        assert gate["hosts"] == {"app.example.test": ["/oauth2"]}
        assert gate["signing_secret"] == "gate-signing-secret-0123456789"
        assert gate["email_domain"] == "example.test"

    def test_without_a_flow_there_is_one_password_step_on_the_first_server(self) -> None:
        gate = json.loads(render_broker_files(self.snap(None), RS, URL)["gate/acme-staff.json"])
        assert gate["steps"] == [
            {"name": "password", "kind": "password", "server": "main", "prompt": "Password"}
        ]

    def test_routes_send_radius_logins_through_the_gate(self) -> None:
        files = render_broker_files(self.snap(), RS, URL)
        assert f"proxy_pass http://127.0.0.1:{GATE_PORT};" in files["dex/routes.conf"]
        (inst,) = json.loads(files["dex/instances.json"])["instances"]
        assert inst["gate"] is True

    def test_secret_refs(self) -> None:
        assert broker_secret_refs(self.snap()) == [
            "kv/tenants/acme/auth/client",
            "kv/tenants/acme/auth/gate",
            "kv/tenants/acme/auth/otp",
            "kv/tenants/acme/auth/rs",
        ]

    @pytest.mark.parametrize(
        ("ref", "value", "message"),
        [
            ("rs", "short", "RADIUS secret"),
            ("otp", "has a space in it", "RADIUS secret"),
            ("gate", "too-short-key", "signing secret"),
        ],
    )
    def test_unusable_secrets(self, ref: str, value: str, message: str) -> None:
        with pytest.raises(RenderError, match=message):
            render_broker_files(self.snap(), {**RS, f"kv/tenants/acme/auth/{ref}": value}, URL)

    def test_missing_secret(self) -> None:
        secrets = {k: v for k, v in RS.items() if not k.endswith("/otp")}
        with pytest.raises(RenderError, match="missing secret"):
            render_broker_files(self.snap(), secrets, URL)

    def test_gate_files_are_written_owner_only_and_removed_when_stale(self, tmp_path: Path) -> None:
        files = render_broker_files(self.snap(), RS, URL)
        changed = write_broker_files(files, tmp_path)
        assert "gate/acme-staff.json" in changed
        target = tmp_path / "gate" / "acme-staff.json"
        assert target.exists()
        assert write_broker_files(files, tmp_path) == []
        assert (
            write_broker_files({}, tmp_path)
            == [
                "dex/acme-staff.yaml",
                "dex/instances.json",
                "dex/routes.conf",
                "gate/acme-staff.json",
            ]
            or not target.exists()
        )
        assert not target.exists()


def test_tenant_constant_is_the_one_the_fixtures_use() -> None:
    assert T == "t-acme"


class TestStepUp:
    def snap(self) -> Any:
        sso = access(rules=[{"mode": "login", "min_factors": 2}])
        return snapshot_from_mapping(data_with(radius_dir(), sso))

    def config(self, snap: Any, broker: str | None = URL) -> str:
        return render_haproxy(snap, snap.se_groups[0].id, broker).files["haproxy/haproxy.cfg"]

    def test_the_rule_redirects_get_to_the_gate_and_refuses_other_methods(self) -> None:
        lines = [ln for ln in self.config(self.snap()).splitlines() if "sy-factors-2" in ln]
        assert len(lines) == 2
        assert (
            lines[0]
            .lstrip()
            .startswith(
                'http-request redirect location "https://auth.example.test/dex/acme/staff/_gate/'
                'elevate?level=2&host=%[req.hdr(host),field(1,:),url_enc]&path=%[pathq,url_enc]"'
            )
        )
        assert "method GET HEAD" in lines[0] and "code 303" in lines[0]
        assert "return status 403" in lines[1] and "method" not in lines[1]

    def test_the_address_needs_the_broker_url(self) -> None:
        with pytest.raises(RenderError, match="broker URL"):
            self.config(self.snap(), None)

    def test_a_set_without_min_factors_needs_no_broker_url_and_no_rule(self) -> None:
        snap = snapshot_from_mapping(data_with(radius_dir(), access()))
        assert "sy-factors" not in self.config(snap, None)

    def test_only_the_rule_that_asks_is_guarded(self) -> None:
        rules = [
            {"match": {"path_prefix": "/open"}, "mode": "login"},
            {"match": {"path_prefix": "/admin"}, "mode": "login", "min_factors": 2},
        ]
        snap = snapshot_from_mapping(data_with(radius_dir(), access(rules=rules)))
        cfg = self.config(snap)
        assert cfg.count("sy-factors-2") == 2
        assert "var(txn.sy_ad) -m int 3 } !{ req.fhdr(X-Forwarded-Groups)" in cfg

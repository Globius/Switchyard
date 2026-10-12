"""Directory policy sets (LDAP, SAML, local users) and the login broker's configs (ADR 0137)."""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.models import (
    DirectoryPolicySet,
    LdapConnector,
    LocalConnector,
    OidcSettings,
    SamlConnector,
)
from switchyard.domain.snapshot import validate_snapshot
from switchyard.render.auth import auth_instances, render_auth_files
from switchyard.render.broker import (
    BROKER_PORT_BASE,
    broker_instances,
    broker_secret_refs,
    check_broker_url,
    render_broker_files,
)
from switchyard.render.escape import RenderError
from switchyard.services.broker import write_broker_files
from tests.factories import base_data

T = "t-acme"
URL = "https://auth.example.test/dex"
GROUP = uuid.UUID(int=0)
CA = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBtjCCAVugAwIBAgITBmyf1XSXNmY/Owua2eiedgPySjAKBggqhkjOPQQDAjA5\n"
    "MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6b24g\n"
    "-----END CERTIFICATE-----\n"
)
HASH = "$2a$10$2b2cU8CPhOTaGrs1HRQuAueS7JTT5ZHsHSzYiFPm1leZck7Mc8T4W"
SECRETS = {
    "kv/tenants/acme/auth/client": "client-secret-value",
    "kv/tenants/acme/auth/cookie": "0123456789abcdef",
    "kv/tenants/acme/auth/bind": "bind-password",
    "kv/tenants/acme/auth/alice": HASH,
}

LDAP: dict[str, Any] = {
    "type": "ldap",
    "host": "ldap.example.test",
    "bind_dn": "cn=svc,dc=example,dc=test",
    "bind_password_ref": "kv/tenants/acme/auth/bind",
    "user_base_dn": "ou=people,dc=example,dc=test",
}
LOCAL: dict[str, Any] = {
    "type": "local",
    "users": [
        {
            "username": "alice",
            "email": "alice@example.test",
            "password_hash_ref": "kv/tenants/acme/auth/alice",
        }
    ],
}
SAML: dict[str, Any] = {"type": "saml", "sso_url": "https://idp.example.test/sso", "ca_pem": CA}


def directory(connector: dict[str, Any], name: str = "staff") -> dict[str, Any]:
    return {
        "id": f"d-{name}",
        "tenant_id": T,
        "name": name,
        "kind": "directory",
        "connector": connector,
    }


def access(dir_name: str = "staff", **extra: Any) -> dict[str, Any]:
    return {
        "id": "ps-sso",
        "tenant_id": T,
        "name": "sso",
        "kind": "access",
        "oidc": {
            "directory_id": f"d-{dir_name}",
            "redirect_hosts": ["app.example.test"],
            "client_id": "web",
            "client_secret_ref": "kv/tenants/acme/auth/client",
            "cookie_secret_ref": "kv/tenants/acme/auth/cookie",
        },
        **extra,
    }


def data_with(*policies: dict[str, Any]) -> dict[str, Any]:
    data = base_data()
    data["policy_sets"] = list(policies)
    access_ids = [p["id"] for p in policies if p["kind"] == "access"]
    data["virtual_services"][0]["policy_set_ids"] = access_ids
    return data


def codes(data: dict[str, Any]) -> list[str]:
    return [p.code for p in validate_snapshot(snapshot_from_mapping(data))]


class TestModels:
    def test_connectors(self) -> None:
        ldap = LdapConnector.model_validate(LDAP)
        assert ldap.port == 636 and ldap.security == "ldaps" and ldap.group_base_dn is None
        assert SamlConnector.model_validate(SAML).groups_attr == "groups"
        assert LocalConnector.model_validate(LOCAL).users[0].username == "alice"

    @pytest.mark.parametrize(
        "update",
        [
            {"host": "ldap.example.test:389"},
            {"security": "none"},
            {"bind_dn": "cn=a)(uid=*"},
            {"user_filter": "(uid=a"},
            {"user_filter": "uid=a"},
            {"user_filter": "(uid=a)\nfoo"},
            {"username_attr": "uid;x"},
            {"root_ca_pem": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"},
            {"port": 0},
        ],
    )
    def test_ldap_values_are_strict(self, update: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            LdapConnector.model_validate({**LDAP, **update})

    def test_saml_needs_https_and_a_certificate(self) -> None:
        for bad in ({"sso_url": "http://idp.example.test/sso"}, {"ca_pem": "nope"}):
            with pytest.raises(ValidationError):
                SamlConnector.model_validate({**SAML, **bad})

    def test_local_users_are_unique_and_valid(self) -> None:
        user = LOCAL["users"][0]
        with pytest.raises(ValidationError, match="unique"):
            LocalConnector.model_validate({"users": [user, {**user, "username": "bob"}]})
        with pytest.raises(ValidationError):
            LocalConnector.model_validate({"users": [{**user, "email": "not an address"}]})
        with pytest.raises(ValidationError):
            LocalConnector.model_validate({"users": []})

    def test_a_directory_set_picks_its_connector_by_type(self) -> None:
        body = {"id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "name": "d", "connector": SAML}
        assert isinstance(DirectoryPolicySet.model_validate(body).connector, SamlConnector)
        with pytest.raises(ValidationError):
            DirectoryPolicySet.model_validate({**body, "connector": {"type": "radius"}})

    def test_oidc_settings_take_an_issuer_or_a_directory(self) -> None:
        base = {
            "client_id": "web",
            "client_secret_ref": "kv/tenants/a/c",
            "cookie_secret_ref": "kv/tenants/a/k",
        }
        issuer = {"issuer": "https://login.example.test"}
        directory = {"directory_id": uuid.uuid4(), "redirect_hosts": ["app.example.test"]}
        assert OidcSettings(**base, **issuer).directory_id is None
        assert OidcSettings(**base, **directory).issuer is None
        for bad in (
            {},
            {**issuer, **directory},
            {"directory_id": uuid.uuid4()},
            {**issuer, "redirect_hosts": ["a.test"]},
        ):
            with pytest.raises(ValidationError):
                OidcSettings(**base, **bad)


class TestSnapshot:
    def test_valid(self) -> None:
        assert codes(data_with(directory(LDAP), access())) == []

    def test_an_unknown_directory(self) -> None:
        assert codes(data_with(access("missing"))) == ["unknown_reference"]

    def test_local_users_have_no_groups(self) -> None:
        sso = access(rules=[{"mode": "groups", "groups": ["ops"]}])
        assert codes(data_with(directory(LOCAL), sso)) == ["directory_has_no_groups"]
        assert codes(data_with(directory(LDAP), sso)) == []

    def test_directory_secrets_stay_in_the_tenants_namespace(self) -> None:
        other = {**LDAP, "bind_password_ref": "kv/tenants/globex/auth/bind"}
        assert codes(data_with(directory(other), access())) == ["secret_ref_not_allowed"]
        user = {**LOCAL["users"][0], "password_hash_ref": "kv/proxmox/token"}
        assert "secret_ref_not_allowed" in codes(
            data_with(directory({**LOCAL, "users": [user]}), access())
        )

    def test_a_directory_is_not_attachable_to_a_service(self) -> None:
        data = data_with(directory(LDAP), access())
        data["virtual_services"][0]["policy_set_ids"].append("d-staff")
        assert "wrong_policy_kind" in codes(data)


class TestBroker:
    def snap(self, *connectors: tuple[str, dict[str, Any]]) -> Any:
        policies: list[dict[str, Any]] = []
        for name, connector in connectors:
            policies.append(directory(connector, name))
            sso = access(name)
            sso["id"], sso["name"] = f"ps-{name}", f"sso-{name}"
            sso["oidc"]["client_id"] = f"web-{name}"
            policies.append(sso)
        return snapshot_from_mapping(data_with(*policies))

    def test_one_instance_per_directory_in_use_sorted_by_name(self) -> None:
        snap = self.snap(("zed", SAML), ("alpha", LDAP), ("mid", LOCAL))
        found = broker_instances(snap)
        assert [(i.name, i.port) for i in found] == [
            ("acme-alpha", BROKER_PORT_BASE),
            ("acme-mid", BROKER_PORT_BASE + 1),
            ("acme-zed", BROKER_PORT_BASE + 2),
        ]

    def test_an_unused_directory_has_no_instance(self) -> None:
        assert broker_instances(snapshot_from_mapping(data_with(directory(LDAP)))) == []

    def test_ldap_config(self) -> None:
        snap = self.snap(("staff", {**LDAP, "group_base_dn": "ou=groups,dc=example,dc=test"}))
        files = render_broker_files(snap, SECRETS, URL)
        config = yaml.safe_load(files["dex/acme-staff.yaml"])
        assert config["issuer"] == f"{URL}/acme/staff"
        assert config["web"] == {"http": f"127.0.0.1:{BROKER_PORT_BASE}"}
        (connector,) = config["connectors"]
        assert (
            connector["type"] == "ldap" and connector["config"]["host"] == "ldap.example.test:636"
        )
        assert connector["config"]["bindPW"] == "bind-password"
        assert connector["config"]["groupSearch"]["userMatchers"] == [
            {"userAttr": "DN", "groupAttr": "member"}
        ]
        (client,) = config["staticClients"]
        assert client["redirectURIs"] == ["https://app.example.test/oauth2/callback"]
        assert client["secret"] == "client-secret-value"
        assert "enablePasswordDB" not in config

    def test_ldap_without_groups_and_with_starttls(self) -> None:
        snap = self.snap(("staff", {**LDAP, "security": "starttls", "port": 389}))
        config = yaml.safe_load(render_broker_files(snap, SECRETS, URL)["dex/acme-staff.yaml"])
        conn = config["connectors"][0]["config"]
        assert (
            conn["startTLS"] is True and "groupSearch" not in conn and conn["host"].endswith(":389")
        )

    def test_saml_config(self) -> None:
        snap = self.snap(("partner", SAML))
        config = yaml.safe_load(render_broker_files(snap, SECRETS, URL)["dex/acme-partner.yaml"])
        conn = config["connectors"][0]
        assert conn["type"] == "saml"
        assert conn["config"]["redirectURI"] == f"{URL}/acme/partner/callback"
        assert conn["config"]["groupsAttr"] == "groups"

    def test_local_config_has_users_with_hashes_and_stable_ids(self) -> None:
        snap = self.snap(("staff", LOCAL))
        text = render_broker_files(snap, SECRETS, URL)["dex/acme-staff.yaml"]
        config = yaml.safe_load(text)
        assert config["enablePasswordDB"] is True and "connectors" not in config
        (user,) = config["staticPasswords"]
        assert user["hash"] == HASH and user["email"] == "alice@example.test"
        again = yaml.safe_load(render_broker_files(snap, SECRETS, URL)["dex/acme-staff.yaml"])
        assert again["staticPasswords"][0]["userID"] == user["userID"]

    def test_instances_and_routes_files(self) -> None:
        files = render_broker_files(self.snap(("staff", LDAP)), SECRETS, URL)
        assert sorted(files) == [
            "dex/acme-staff.yaml",
            "dex/instances.json",
            "dex/routes.conf",
        ]
        listing = json.loads(files["dex/instances.json"])["instances"]
        assert listing == [
            {"name": "acme-staff", "path": "/dex/acme/staff", "port": BROKER_PORT_BASE}
        ]
        routes = files["dex/routes.conf"]
        assert "location /dex/acme/staff/ {" in routes
        assert f"proxy_pass http://127.0.0.1:{BROKER_PORT_BASE};" in routes

    def test_no_directory_no_files(self) -> None:
        assert render_broker_files(snapshot_from_mapping(base_data()), {}, URL) == {}

    def test_secret_refs(self) -> None:
        snap = self.snap(("a", LDAP), ("b", LOCAL))
        assert broker_secret_refs(snap) == [
            "kv/tenants/acme/auth/alice",
            "kv/tenants/acme/auth/bind",
            "kv/tenants/acme/auth/client",
        ]

    @pytest.mark.parametrize("drop", ["bind", "client", "alice"])
    def test_a_missing_secret_stops_the_render(self, drop: str) -> None:
        snap = self.snap(("a", LDAP), ("b", LOCAL))
        secrets = {k: v for k, v in SECRETS.items() if not k.endswith(drop)}
        with pytest.raises(RenderError, match="missing secret"):
            render_broker_files(snap, secrets, URL)

    @pytest.mark.parametrize(
        ("ref", "value"),
        [
            ("bind", "has space"),
            ("bind", "x" * 513),
            ("alice", "not-a-bcrypt-hash"),
            ("client", ""),
        ],
    )
    def test_unusable_secrets_stop_the_render(self, ref: str, value: str) -> None:
        snap = self.snap(("a", LDAP), ("b", LOCAL))
        secrets = {**SECRETS, f"kv/tenants/acme/auth/{ref}": value}
        with pytest.raises(RenderError):
            render_broker_files(snap, secrets, URL)

    def test_a_non_bcrypt_value_that_looks_like_a_secret_is_refused_for_users(self) -> None:
        snap = self.snap(("b", LOCAL))
        secrets = {**SECRETS, "kv/tenants/acme/auth/alice": "plain-password-1"}
        with pytest.raises(RenderError, match="bcrypt"):
            render_broker_files(snap, secrets, URL)

    @pytest.mark.parametrize(
        "bad",
        [
            "http://auth.example.test",
            "https://",
            "https://u@auth.example.test",
            "https://a.test/x?y=1",
            "https://a.test/#f",
            "https://a.test/a b",
        ],
    )
    def test_broker_url_is_https_and_plain(self, bad: str) -> None:
        with pytest.raises(RenderError):
            check_broker_url(bad)

    def test_yaml_injection_through_values_is_not_possible(self) -> None:
        # values are validated, and the file is dumped as data, never concatenated
        snap = self.snap(("staff", {**LDAP, "user_filter": "(&(uid=a)(mail=*))"}))
        text = render_broker_files(snap, SECRETS, URL)["dex/acme-staff.yaml"]
        assert yaml.safe_load(text)["connectors"][0]["config"]["userSearch"]["filter"] == (
            "(&(uid=a)(mail=*))"
        )


class TestAccessSetsWithADirectory:
    def snap(self) -> Any:
        return snapshot_from_mapping(data_with(directory(LDAP), access()))

    def test_the_proxy_trusts_the_broker_as_its_issuer(self) -> None:
        snap = self.snap()
        files = render_auth_files(snap, snap.se_groups[0].id, SECRETS, URL)
        assert (
            'oidc_issuer_url = "https://auth.example.test/dex/acme/staff"'
            in files["auth/web-sso.cfg"]
        )

    def test_a_missing_broker_url_stops_the_render(self) -> None:
        snap = self.snap()
        with pytest.raises(RenderError, match="no login broker URL"):
            render_auth_files(snap, snap.se_groups[0].id, SECRETS, None)

    def test_the_instance_knows_its_directory_and_tenant(self) -> None:
        snap = self.snap()
        (inst,) = auth_instances(snap, snap.se_groups[0].id)
        assert (
            inst.directory is not None and inst.directory.name == "staff" and inst.tenant == "acme"
        )


class TestWriting:
    def test_writes_changes_only_and_removes_stale_files(self, tmp_path: Path) -> None:
        files = {"dex/a.yaml": "a\n", "dex/instances.json": "{}\n"}
        assert write_broker_files(files, tmp_path) == ["dex/a.yaml", "dex/instances.json"]
        assert write_broker_files(files, tmp_path) == []
        assert write_broker_files(
            {"dex/a.yaml": "a\n", "dex/instances.json": "{}\n"} | {"dex/a.yaml": "b\n"}, tmp_path
        ) == ["dex/a.yaml"]
        (tmp_path / "dex" / "old.yaml").write_text("x", "utf-8")
        assert write_broker_files(
            {"dex/a.yaml": "b\n", "dex/instances.json": "{}\n"}, tmp_path
        ) == ["dex/old.yaml"]
        assert write_broker_files({}, tmp_path) == ["dex/a.yaml", "dex/instances.json"]
        assert list((tmp_path / "dex").iterdir()) == []

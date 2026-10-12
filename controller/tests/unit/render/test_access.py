"""Access policy sets: model rules, snapshot checks, HAProxy frontend rules and proxy configs."""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.domain.models import AccessPolicySet, AccessRule, IdentityHeaders, OidcSettings
from switchyard.domain.snapshot import validate_snapshot
from switchyard.domain.validators import validate_group_name, validate_issuer
from switchyard.render.auth import (
    AUTH_PORT_BASE,
    LOOP_PORT_BASE,
    auth_instances,
    auth_secret_refs,
    render_auth_files,
)
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import render_haproxy
from tests.factories import base_data

T = "t-acme"
GROUP = uuid.UUID(symbolic_uuid("seg-1"))
CLIENT = "kv/tenants/acme/auth/client"
COOKIE = "kv/tenants/acme/auth/cookie"
CERT = "kv/tenants/acme/certs/wild-example-test"
PEM = "-----BEGIN CERTIFICATE-----" + chr(10) + "-----BEGIN PRIVATE KEY-----" + chr(10)
SECRETS = {CLIENT: "client-secret-value", COOKIE: "0123456789abcdef"}


def access(**extra: Any) -> dict[str, Any]:
    return {
        "id": "ps-sso",
        "tenant_id": T,
        "name": "sso",
        "kind": "access",
        "oidc": {
            "issuer": "https://login.example.test/realms/acme",
            "client_id": "web",
            "client_secret_ref": CLIENT,
            "cookie_secret_ref": COOKIE,
        },
        **extra,
    }


def with_access(*policies: dict[str, Any]) -> dict[str, Any]:
    data = base_data()
    data["policy_sets"] = list(policies)
    data["virtual_services"][0]["policy_set_ids"] = [p["id"] for p in policies]
    return data


def codes(data: dict[str, Any]) -> list[str]:
    return [p.code for p in validate_snapshot(snapshot_from_mapping(data))]


def frontend(data: dict[str, Any]) -> list[str]:
    text = render_haproxy(snapshot_from_mapping(data), GROUP).files["haproxy/haproxy.cfg"]
    block = text.split("frontend vs_web\n", 1)[1].split("\n\n", 1)[0]
    return [line.strip() for line in block.splitlines()]


class TestValidators:
    @pytest.mark.parametrize(
        "issuer",
        ["https://login.example.test", "https://login.example.test:8443/realms/a", "https://a.b/c"],
    )
    def test_issuers(self, issuer: str) -> None:
        assert validate_issuer(issuer) == issuer

    @pytest.mark.parametrize(
        "issuer",
        [
            "http://login.example.test",
            "https://",
            "https://user@login.example.test",
            "https://login.example.test/a b",
            'https://login.example.test/a"b',
            "https://login.example.test/a?b=1",
            "https://login.example.test/#x",
            "https://1.2.3.4/a",
            "javascript:alert(1)",
        ],
    )
    def test_bad_issuers(self, issuer: str) -> None:
        with pytest.raises(ValueError):
            validate_issuer(issuer)

    @pytest.mark.parametrize("group", ["ops.team", "/staff/admins", "a@b=c", "00000000-0000"])
    def test_groups(self, group: str) -> None:
        assert validate_group_name(group) == group

    @pytest.mark.parametrize("group", ["", "a b", "a'b", 'a"b', "a|b", "a(b)", "a,b", "a\nb"])
    def test_bad_groups(self, group: str) -> None:
        with pytest.raises(ValueError):
            validate_group_name(group)


class TestModel:
    def oidc(self, **kw: Any) -> OidcSettings:
        base = {
            "issuer": "https://login.example.test",
            "client_id": "web",
            "client_secret_ref": CLIENT,
            "cookie_secret_ref": COOKIE,
        }
        return OidcSettings(**{**base, **kw})

    def test_defaults_are_closed_and_signed_cookies(self) -> None:
        o = self.oidc()
        assert o.scopes == ("openid", "email", "profile") and o.cookie_refresh_s < o.cookie_expire_s

    def test_openid_scope_is_required(self) -> None:
        with pytest.raises(ValidationError, match="openid"):
            self.oidc(scopes=["email"])

    def test_refresh_must_be_shorter_than_the_lifetime(self) -> None:
        with pytest.raises(ValidationError, match="less than"):
            self.oidc(cookie_expire_s=600, cookie_refresh_s=600)

    @pytest.mark.parametrize("scope", ["a b", "a'b", "a;b", ""])
    def test_scopes_are_plain(self, scope: str) -> None:
        with pytest.raises(ValidationError):
            self.oidc(scopes=["openid", scope])

    def test_secret_refs_must_be_paths(self) -> None:
        with pytest.raises(ValidationError):
            self.oidc(client_secret_ref="hunter2 plain secret")

    def test_rule_modes_and_their_fields(self) -> None:
        assert AccessRule(mode="groups", groups=["a"]).mode == "groups"
        assert AccessRule(mode="email_domains", email_domains=["example.test"])
        for bad in (
            {"mode": "groups"},
            {"mode": "email_domains"},
            {"mode": "login", "groups": ["a"]},
            {"mode": "groups", "groups": ["a"], "email_domains": ["a.test"]},
        ):
            with pytest.raises(ValidationError):
                AccessRule(**bad)

    def test_identity_headers_cannot_use_protected_names(self) -> None:
        assert IdentityHeaders(user="X-User", email=None, groups=None).email is None
        for name in ("Authorization", "Host", "X-Forwarded-For", "X-Forwarded-User", "Connection"):
            with pytest.raises(ValidationError, match="cannot carry identity"):
                IdentityHeaders(user=name)
        with pytest.raises(ValidationError, match="different"):
            IdentityHeaders(user="X-A", email="x-a")

    def test_auth_path(self) -> None:
        base = access()
        for bad in ("/", "/oauth2/", "oauth2"):
            with pytest.raises(ValidationError):
                AccessPolicySet.model_validate(
                    {**base, "id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "auth_path": bad}
                )


class TestSnapshot:
    def test_valid_and_secret_scope(self) -> None:
        assert codes(with_access(access())) == []
        other = access()
        other["oidc"] = {**other["oidc"], "client_secret_ref": "kv/tenants/globex/auth/c"}
        assert codes(with_access(other)) == ["secret_ref_not_allowed"]

    def test_one_access_set_per_service(self) -> None:
        second = {**access(), "id": "ps-2", "name": "second"}
        assert "multiple_access_sets" in codes(with_access(access(), second))

    def test_plain_ports_must_redirect_to_tls(self) -> None:
        data = with_access(access())
        data["virtual_services"][0]["ports"] = [{"port": 443, "tls": True}, {"port": 8080}]
        assert codes(data) == ["access_needs_tls"]
        data["virtual_services"][0]["ports"] = [
            {"port": 443, "tls": True},
            {"port": 8080, "https_redirect_port": 443},
        ]
        assert codes(data) == []


class TestFrontend:
    def test_loopback_bind_and_entry(self) -> None:
        lines = frontend(with_access(access()))
        assert f"bind 127.0.0.1:{LOOP_PORT_BASE}" in lines
        assert (
            f"http-request set-var(txn.sy_loop) bool(true) if {{ dst 127.0.0.1 }} "
            f"{{ dst_port {LOOP_PORT_BASE} }}" in lines
        )
        assert "http-request set-src hdr(X-Forwarded-For) if { var(txn.sy_loop) -m bool }" in lines

    def test_client_identity_headers_are_dropped_before_anything_else_reads_them(self) -> None:
        lines = frontend(with_access(access()))
        dropped = [ln.split()[2] for ln in lines if ln.startswith("http-request del-header")]
        for name in (
            "X-Auth-User",
            "X-Auth-Email",
            "X-Auth-Groups",
            "X-Forwarded-User",
            "X-Sy-Access",
        ):
            assert name in dropped
        first_drop = next(i for i, ln in enumerate(lines) if "del-header X-Auth-User" in ln)
        first_use = next(i for i, ln in enumerate(lines) if "set-header X-Sy-Access" in ln)
        assert first_drop < first_use

    def test_decision_values_follow_rule_order(self) -> None:
        policy = access(
            default_mode="login",
            rules=[
                {"match": {"path_prefix": "/health"}, "mode": "public"},
                {"match": {"path_prefix": "/a"}, "mode": "groups", "groups": ["g.1", "g2"]},
                {"match": {"path_prefix": "/b"}, "mode": "login"},
            ],
        )
        lines = frontend(with_access(policy))
        sets = [ln for ln in lines if "set-var(txn.sy_ad) int(" in ln]
        assert [ln.split("int(")[1].split(")")[0] for ln in sets] == ["1", "3", "4", "5"]
        assert (
            "http-request return status 403 if { var(txn.sy_loop) -m bool } { var(txn.sy_ad) -m int 3 } !{ req.fhdr(X-Forwarded-Groups) -m reg '(^|,)(g2|g[.]1)(,|$)' }"
            in lines
        )

    def test_public_default(self) -> None:
        lines = frontend(with_access(access(default_mode="public")))
        sets = [ln for ln in lines if "set-var(txn.sy_ad) int(" in ln]
        assert [ln.split("int(")[1].split(")")[0] for ln in sets] == ["1"]

    def test_second_hop_needs_the_decision_header(self) -> None:
        lines = frontend(with_access(access()))
        assert (
            "http-request return status 403 if { var(txn.sy_loop) -m bool } "
            "!{ req.hdr(X-Sy-Access) -m found }" in lines
        )
        assert (
            "http-request return status 401 if { var(txn.sy_loop) -m bool } "
            "{ var(txn.sy_ad) -m int 2: } !{ req.hdr(X-Forwarded-User) -m found }" in lines
        )

    def test_protected_requests_go_to_the_proxy_first(self) -> None:
        lines = frontend(with_access(access()))
        uses = [ln for ln in lines if ln.startswith("use_backend")]
        assert uses[0].startswith("use_backend auth_web-sso if !{ var(txn.sy_loop)")
        assert "path_beg /oauth2/" in uses[0]
        assert "-m int 2:" in uses[1]

    def test_stages_run_once_on_the_first_hop(self) -> None:
        data = with_access(
            access(),
            {
                "id": "ps-rw",
                "tenant_id": T,
                "name": "rw",
                "kind": "rewrite",
                "rules": [
                    {"action": {"type": "set_header", "name": "X-A", "value": "1"}},
                    {
                        "phase": "response",
                        "action": {"type": "set_header", "name": "X-R", "value": "1"},
                    },
                ],
            },
        )
        lines = frontend(data)
        guard = "!{ var(txn.sy_loop) -m bool }"
        assert f'http-request set-header X-A "1" if {guard}' in lines
        assert any(
            ln.startswith('http-response set-header X-R "1" if { var(txn.sy_m1)') for ln in lines
        )
        assert f"http-request set-var(txn.sy_m1) bool(true) if {guard}" in lines

    def test_proxy_backend_is_closed_by_default(self) -> None:
        text = render_haproxy(snapshot_from_mapping(with_access(access())), GROUP).files[
            "haproxy/haproxy.cfg"
        ]
        assert (
            f"backend auth_web-sso\n    mode http\n    server auth 127.0.0.1:{AUTH_PORT_BASE}\n"
            in text
        )

    def test_allow_list_enforcement_exempts_the_loopback_hop(self) -> None:
        data = with_access(access())
        data["policy_sets"].append(
            {
                "id": "ps-al",
                "tenant_id": T,
                "name": "al",
                "kind": "allow_list",
                "cidrs": ["10.0.0.0/8"],
            }
        )
        data["virtual_services"][0].update(allow_list_id="ps-al", enforce_allow_list=True)
        text = render_haproxy(snapshot_from_mapping(data), GROUP).files["haproxy/haproxy.cfg"]
        assert (
            "tcp-request session reject if !{ src -f /etc/switchyard/haproxy/lists/al.cidr } !{ dst 127.0.0.1 }"
            in text
        )

    def test_service_without_access_is_unchanged(self) -> None:
        lines = frontend(base_data())
        assert not any("sy_loop" in ln or "sy_ad" in ln for ln in lines)

    def test_hostile_group_never_reaches_the_regex(self) -> None:
        snap = snapshot_from_mapping(
            with_access(access(rules=[{"mode": "groups", "groups": ["g"]}]))
        )
        policy = snap.policy_sets[0]
        assert isinstance(policy, AccessPolicySet)
        rule = policy.rules[0].model_copy(update={"groups": ("g)|(x'",)})
        evil = snap.model_copy(
            update={"policy_sets": (policy.model_copy(update={"rules": (rule,)}),)}
        )
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)


class TestInstances:
    def test_ports_follow_service_name_order(self) -> None:
        data = with_access(access())
        second = {**data["virtual_services"][0], "id": "vs-0", "name": "api", "vip": "192.0.2.21"}
        second["ports"] = [{"port": 443, "tls": True}]
        data["virtual_services"].append(second)
        found = auth_instances(snapshot_from_mapping(data), GROUP)
        assert [(i.service.name, i.auth_port, i.loop_port) for i in found] == [
            ("api", AUTH_PORT_BASE, LOOP_PORT_BASE),
            ("web", AUTH_PORT_BASE + 1, LOOP_PORT_BASE + 1),
        ]
        assert [i.path for i in found] == ["auth/api-sso.cfg", "auth/web-sso.cfg"]

    def test_disabled_services_have_no_instance(self) -> None:
        data = with_access(access())
        data["virtual_services"][0]["enabled"] = False
        assert auth_instances(snapshot_from_mapping(data), GROUP) == []

    def test_proxy_config(self) -> None:
        snap = snapshot_from_mapping(
            with_access(access(identity_headers={"token": "X-Auth-Token"}))
        )
        assert auth_secret_refs(snap, GROUP) == sorted([CLIENT, COOKIE])
        files = render_auth_files(snap, GROUP, SECRETS)
        assert list(files) == ["auth/web-sso.cfg", "auth/web-sso.env"]
        assert files["auth/web-sso.env"].splitlines() == [
            "OAUTH2_PROXY_CLIENT_SECRET=client-secret-value",
            "OAUTH2_PROXY_COOKIE_SECRET=0123456789abcdef",
        ]
        text = files["auth/web-sso.cfg"]
        assert "secret-value" not in text and "0123456789abcdef" not in text
        for line in (
            'oidc_issuer_url = "https://login.example.test/realms/acme"',
            f'http_address = "127.0.0.1:{AUTH_PORT_BASE}"',
            f'upstreams = ["http://127.0.0.1:{LOOP_PORT_BASE}"]',
            "cookie_secure = true",
            'cookie_expire = "28800s"',
            "pass_authorization_header = true",
            'scope = "openid email profile"',
        ):
            assert line in text.splitlines()

    def test_no_token_means_no_authorization_header(self) -> None:
        snap = snapshot_from_mapping(with_access(access()))
        text = render_auth_files(snap, GROUP, SECRETS)["auth/web-sso.cfg"]
        assert "pass_authorization_header = false" in text

    @pytest.mark.parametrize(
        "secrets",
        [
            {CLIENT: "client-secret-value"},
            {CLIENT: "client-secret-value", COOKIE: "short"},
            {CLIENT: "client-secret-value", COOKIE: "0123456789abcde"},
            {CLIENT: 'has"quote-chars', COOKIE: "0123456789abcdef"},
            {CLIENT: "x" * 513, COOKIE: "0123456789abcdef"},
        ],
    )
    def test_bad_secrets_stop_the_render(self, secrets: dict[str, str]) -> None:
        snap = snapshot_from_mapping(with_access(access()))
        with pytest.raises(RenderError):
            render_auth_files(snap, GROUP, secrets)

    def test_nothing_without_access_sets(self) -> None:
        snap = snapshot_from_mapping(base_data())
        assert render_auth_files(snap, GROUP, {}) == {}
        assert auth_secret_refs(snap, GROUP) == []


class TestBundle:
    """The engine bundle carries the proxy configs and fails when a secret is missing."""

    async def test_files_are_in_the_bundle(self) -> None:
        from switchyard.adapters.secrets import MemorySecrets
        from switchyard.pipeline.bundles import engine_files
        from switchyard.render.routing import EngineSlot

        snap = snapshot_from_mapping(with_access(access()))
        slot = EngineSlot("se-1", 0, "10.0.0.5")
        files = await engine_files(
            snap, snap.se_groups[0], slot, MemorySecrets({**SECRETS, CERT: PEM})
        )
        assert "auth/web-sso.cfg" in files
        assert "OAUTH2_PROXY_CLIENT_SECRET=client-secret-value" in files["auth/web-sso.env"]

    async def test_a_missing_secret_stops_the_rollout(self) -> None:
        from switchyard.adapters.secrets import MemorySecrets
        from switchyard.pipeline.bundles import BundleError, engine_files
        from switchyard.render.routing import EngineSlot

        snap = snapshot_from_mapping(with_access(access()))
        slot = EngineSlot("se-1", 0, "10.0.0.5")
        with pytest.raises(BundleError, match="not found"):
            await engine_files(
                snap, snap.se_groups[0], slot, MemorySecrets({CLIENT: "x" * 20, CERT: PEM})
            )
        with pytest.raises(BundleError, match="no secrets backend"):
            await engine_files(snap, snap.se_groups[0], slot, None)


class TestPosture:
    RULE = {
        "match": {"path_prefix": "/secure"},
        "mode": "login",
        "posture": {
            "device_certificate": True,
            "claims": [
                {"name": "device_compliant", "op": "true"},
                {"name": "tier", "op": "in", "values": ["gold", "silver"]},
                {"name": "mfa", "op": "present"},
                {"name": "region", "op": "equals", "value": "eu"},
            ],
        },
    }

    def data(self, ca: bool = True) -> dict[str, Any]:
        data = with_access(access(rules=[self.RULE]))
        if ca:
            data["certificates"].append(
                {
                    "id": "ca-1",
                    "tenant_id": T,
                    "name": "devices",
                    "source": "upload",
                    "purpose": "client_ca",
                    "sans": [],
                    "not_after": "2038-01-01T00:00:00+00:00",
                    "secret_ref": "kv/tenants/acme/certs/devices/01",
                }
            )
            data["virtual_services"][0]["ports"][0] = {
                "port": 443,
                "tls": True,
                "client_ca_id": "ca-1",
                "client_verify": "optional",
            }
        return data

    def test_model_rules(self) -> None:
        with pytest.raises(ValidationError):
            AccessRule(mode="public", posture={"device_certificate": True})
        with pytest.raises(ValidationError):
            AccessRule(posture={})
        for bad in (
            {"name": "a", "op": "equals"},
            {"name": "a", "op": "in"},
            {"name": "a", "op": "true", "value": "x"},
            {"name": "a.b", "op": "true"},
            {"name": "a'b", "op": "present"},
        ):
            with pytest.raises(ValidationError):
                AccessRule(posture={"claims": [bad]})

    def test_a_device_certificate_rule_needs_a_client_ca(self) -> None:
        assert codes(self.data()) == []
        assert codes(self.data(ca=False)) == ["posture_needs_client_ca"]

    def test_rendered_checks(self) -> None:
        lines = frontend(self.data())
        here = "{ var(txn.sy_loop) -m bool } { var(txn.sy_ad) -m int 2 }"
        assert (
            "http-request set-header X-Sy-Device 1 if !{ var(txn.sy_loop) -m bool } { ssl_c_used }"
            in lines
        )
        assert (
            "http-request set-var(txn.sy_idt) http_auth_bearer if { var(txn.sy_loop) -m bool }"
            in lines
        )
        assert (
            f"http-request return status 403 if {here} !{{ req.hdr(X-Sy-Device) -m str 1 }}"
            in lines
        )
        fetch = "var(txn.sy_idt),jwt_payload_query('$.{}')"
        assert (
            f"http-request return status 403 if {here} "
            f'!{{ {fetch.format("device_compliant")} -m str "1" "true" }}' in lines
        )
        assert (
            f'http-request return status 403 if {here} !{{ {fetch.format("tier")} -m str "gold" "silver" }}'
            in lines
        )
        assert (
            f"http-request return status 403 if {here} !{{ {fetch.format('mfa')} -m found }}"
            in lines
        )
        assert (
            f'http-request return status 403 if {here} !{{ {fetch.format("region")} -m str "eu" }}'
            in lines
        )

    def test_the_device_header_is_dropped_from_clients_and_the_origin(self) -> None:
        lines = frontend(self.data())
        assert "http-request del-header X-Sy-Device if !{ var(txn.sy_loop) -m bool }" in lines
        assert "http-request del-header X-Sy-Device if { var(txn.sy_loop) -m bool }" in lines

    def test_claims_make_the_proxy_pass_the_token(self) -> None:
        snap = snapshot_from_mapping(self.data())
        text = render_auth_files(snap, GROUP, SECRETS)["auth/web-sso.cfg"]
        assert "pass_authorization_header = true" in text
        assert "http-request del-header Authorization if { var(txn.sy_loop) -m bool }" in "\n".join(
            frontend(self.data())
        )


class TestBooleansCookiesAndProviderCa:
    def test_boolean_spellings(self) -> None:
        from switchyard.render.haproxy import _claim_forms

        assert _claim_forms(("true",)) == ["1", "true"]
        assert _claim_forms(("false", "x")) == ["0", "false", "x"]

    def test_equals_true_matches_a_json_boolean_too(self) -> None:
        rule = {
            "mode": "login",
            "posture": {"claims": [{"name": "email_verified", "op": "equals", "value": "true"}]},
        }
        lines = frontend(with_access(access(rules=[rule])))
        assert any(
            'jwt_payload_query(\'$.email_verified\') -m str "1" "true"' in ln for ln in lines
        )

    def test_session_cookies_are_stripped_on_the_second_hop_only(self) -> None:
        lines = frontend(with_access(access()))  # default name
        strip = [ln for ln in lines if ln.startswith("http-request set-header Cookie ")]
        assert len(strip) == 1 and "(^|; *)_sy_auth(_[A-Za-z0-9]+)*=[^;]*" in strip[0]
        assert strip[0].endswith("if { var(txn.sy_loop) -m bool } { req.hdr(cookie) -m found }")
        assert (
            "http-request del-header Cookie if { var(txn.sy_loop) -m bool } { req.fhdr(cookie) -m len 0 }"
            in lines
        )

    def test_a_provider_ca_goes_to_the_proxy_as_a_file(self) -> None:
        data = with_access(access())
        data["policy_sets"][0]["oidc"]["provider_ca_pem"] = CA_PEM
        snap = snapshot_from_mapping(data)
        files = render_auth_files(snap, GROUP, SECRETS)
        assert files["auth/web-sso-ca.pem"] == CA_PEM
        assert (
            'provider_ca_files = ["/etc/switchyard/auth/web-sso-ca.pem"]'
            in files["auth/web-sso.cfg"]
        )
        plain = render_auth_files(snapshot_from_mapping(with_access(access())), GROUP, SECRETS)
        assert (
            "auth/web-sso-ca.pem" not in plain
            and "provider_ca_files" not in plain["auth/web-sso.cfg"]
        )

    def test_the_provider_ca_must_be_certificates_only(self) -> None:
        data = with_access(access())
        data["policy_sets"][0]["oidc"]["provider_ca_pem"] = (
            "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
        )
        with pytest.raises(ValidationError):
            snapshot_from_mapping(data)


CA_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBtjCCAVugAwIBAgITBmyf1XSXNmY/Owua2eiedgPySjAKBggqhkjOPQQDAjA5\n"
    "MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6b24g\n"
    "-----END CERTIFICATE-----\n"
)

"""JWT policy sets and client-certificate policy sets: models, snapshot checks, rendering, JWKS."""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from joserfc.jwk import ECKey, RSAKey
from pydantic import ValidationError
from typer.testing import CliRunner

from switchyard.cli import app
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.domain.models import (
    CertIdentity,
    ClientCertPolicySet,
    ClientCertRule,
    JwtKey,
    JwtPolicySet,
    JwtRule,
)
from switchyard.domain.snapshot import validate_snapshot
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import render_haproxy
from switchyard.render.tokens import jwt_key_files, key_file
from switchyard.services.jwks import JwksError, jwk_to_key, jwks_to_keys
from tests.factories import base_data

T = "t-acme"
GROUP = uuid.UUID(symbolic_uuid("seg-1"))
RSA = RSAKey.generate_key(2048)
PEM = RSA.as_pem(private=False).decode()
ISSUER = "https://login.example.test/realms/acme"


def jwt_set(*rules: dict[str, Any], name: str = "tok") -> dict[str, Any]:
    return {"id": f"ps-{name}", "tenant_id": T, "name": name, "kind": "jwt", "rules": list(rules)}


def rule(**extra: Any) -> dict[str, Any]:
    return {"issuer": ISSUER, "keys": [{"public_key_pem": PEM}], **extra}


def cert_set(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "ps-cc",
        "tenant_id": T,
        "name": "cc",
        "kind": "client_cert",
        "identities": [{"value": "alice@example.test", "user": "alice", "groups": ["ops"]}],
        "rules": [{"mode": "valid"}],
    }
    return body | extra


def data_with(*policies: dict[str, Any], client_ca: bool = False) -> dict[str, Any]:
    data = base_data()
    data["policy_sets"] = list(policies)
    vs = data["virtual_services"][0]
    vs["policy_set_ids"] = [p["id"] for p in policies]
    if client_ca:
        data["certificates"].append(
            {
                "id": "ca-1",
                "tenant_id": T,
                "name": "staff-ca",
                "source": "upload",
                "purpose": "client_ca",
                "sans": [],
                "not_after": "2038-01-01T00:00:00+00:00",
                "secret_ref": "kv/tenants/acme/certs/staff-ca/01",
            }
        )
        vs["ports"][0] = {"port": 443, "tls": True, "client_ca_id": "ca-1"}
    return data


def codes(data: dict[str, Any]) -> list[str]:
    return [p.code for p in validate_snapshot(snapshot_from_mapping(data))]


def frontend(data: dict[str, Any]) -> list[str]:
    text = render_haproxy(snapshot_from_mapping(data), GROUP).files["haproxy/haproxy.cfg"]
    block = text.split("frontend vs_web\n", 1)[1].split("\n\n", 1)[0]
    return [line.strip() for line in block.splitlines()]


class TestJwtModel:
    def test_defaults(self) -> None:
        r = JwtRule(issuer=ISSUER, keys=[JwtKey(public_key_pem=PEM)])
        assert r.leeway_s == 30 and r.keys[0].alg == "RS256" and r.match.is_empty

    @pytest.mark.parametrize(
        "pem",
        [
            "",
            "-----BEGIN PRIVATE KEY-----\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n-----END PRIVATE KEY-----\n",
            "-----BEGIN PUBLIC KEY-----\nAA\n-----END PUBLIC KEY-----\n",
            PEM + PEM,
            "x" + PEM,
        ],
    )
    def test_only_a_public_key_block_is_accepted(self, pem: str) -> None:
        with pytest.raises(ValidationError):
            JwtKey(public_key_pem=pem)

    def test_a_missing_trailing_newline_is_added(self) -> None:
        assert JwtKey(public_key_pem=PEM.rstrip("\n")).public_key_pem.endswith("\n")

    @pytest.mark.parametrize("alg", ["HS256", "none", "RS1", "rs256"])
    def test_symmetric_and_unknown_algorithms_are_refused(self, alg: str) -> None:
        with pytest.raises(ValidationError):
            JwtKey(alg=alg, public_key_pem=PEM)

    @pytest.mark.parametrize(
        "extra",
        [
            {"issuer": "http://login.example.test"},
            {"audiences": ["a b"]},
            {"audiences": ["x" * 257]},
            {"required_scopes": ["a b"]},
            {"leeway_s": 301},
            {"keys": []},
            {"subject_header": "Authorization"},
            {"subject_header": "X-Forwarded-For"},
        ],
    )
    def test_rule_bounds(self, extra: dict[str, Any]) -> None:
        base: dict[str, Any] = {"issuer": ISSUER, "keys": [{"public_key_pem": PEM}]}
        with pytest.raises(ValidationError):
            JwtRule(**(base | extra))


class TestJwtSnapshotAndRender:
    def test_valid(self) -> None:
        assert codes(data_with(jwt_set(rule()))) == []

    def test_plain_ports_must_redirect_to_tls(self) -> None:
        data = data_with(jwt_set(rule()))
        data["virtual_services"][0]["ports"] = [{"port": 443, "tls": True}, {"port": 8080}]
        assert codes(data) == ["access_needs_tls"]

    def test_the_key_files_are_public_keys_under_certs(self) -> None:
        snap = snapshot_from_mapping(data_with(jwt_set(rule(), rule(), name="api-tokens")))
        files = jwt_key_files(snap, GROUP)
        assert list(files) == ["certs/jwt-api-tokens-1-1.pem", "certs/jwt-api-tokens-2-1.pem"]
        assert all("PUBLIC KEY" in text and "PRIVATE" not in text for text in files.values())
        assert key_file("api-tokens", 2, 1) == "/etc/switchyard/certs/jwt-api-tokens-2-1.pem"

    def test_no_jwt_no_files(self) -> None:
        assert jwt_key_files(snapshot_from_mapping(base_data()), GROUP) == {}

    def test_rule_lines(self) -> None:
        lines = frontend(
            data_with(
                jwt_set(
                    rule(
                        match={"path_prefix": "/api"},
                        audiences=["https://api.example.test/", "api"],
                        required_scopes=["read", "a.b"],
                        leeway_s=5,
                        subject_header="X-Sub",
                        keys=[{"kid": "k1", "alg": "ES256", "public_key_pem": PEM}],
                    )
                )
            )
        )
        text = "\n".join(lines)
        assert "http-request set-var(txn.sy_jtok) http_auth_bearer" in lines
        assert "http-request del-header X-Sub" in lines
        verify = next(ln for ln in lines if ln.startswith("http-request set-var(txn.sy_j1) int(1)"))
        for part in (
            "{ path_beg /api }",
            "jwt_verify(ES256,/etc/switchyard/certs/jwt-tok-1-1.pem) -m int 1",
            "jwt_header_query('$.kid') -m str k1",
            f"jwt_payload_query('$.iss') -m str {ISSUER}",
            "'(^|\")(api|https://api[.]example[.]test/)(\"|$)'",
            "'(^| )a[.]b( |$)'",
            "'(^| )read( |$)'",
            "jwt_payload_query('$.exp','int'),sub(txn.sy_jnow),add(5) -m int 1:",
        ):
            assert part in verify
        assert (
            "http-request return status 401 hdr WWW-Authenticate Bearer if { path_beg /api } "
            "!{ var(txn.sy_jdone) -m found } !{ var(txn.sy_j1) -m found }" in lines
        )
        assert (
            'http-request set-header X-Sub "%[var(txn.sy_jsub1)]" if { var(txn.sy_j1) -m found }'
            in lines
        )
        assert text.index("return status 401") < text.index("set-header X-Sub")

    def test_the_first_matching_rule_decides(self) -> None:
        lines = frontend(data_with(jwt_set(rule(match={"path_prefix": "/a"}), rule())))
        done = [ln for ln in lines if ln.startswith("http-request set-var(txn.sy_jdone)")]
        assert len(done) == 2 and "{ path_beg /a }" in done[0]

    def test_runs_after_fixed_responses_and_before_rewrites(self) -> None:
        respond = {
            "id": "ps-r",
            "tenant_id": T,
            "name": "resp",
            "kind": "respond",
            "rules": [{"match": {"path_exact": "/ping"}, "status": 204}],
        }
        rewrite = {
            "id": "ps-w",
            "tenant_id": T,
            "name": "rw",
            "kind": "rewrite",
            "rules": [{"action": {"type": "set_header", "name": "X-A", "value": "1"}}],
        }
        text = "\n".join(frontend(data_with(jwt_set(rule()), rewrite, respond)))
        assert text.index("status 204") < text.index("http_auth_bearer") < text.index("X-A")

    @pytest.mark.parametrize(
        "update",
        [
            {"issuer": "https://x.test/\nhttp-request deny"},
            {"audiences": ("a b",)},
            {"audiences": ("a'b",)},
            {"required_scopes": ("a b",)},
            {"subject_header": "X A"},
        ],
    )
    def test_hostile_values_stop_the_render(self, update: dict[str, Any]) -> None:
        snap = snapshot_from_mapping(data_with(jwt_set(rule())))
        policy = snap.policy_sets[0]
        assert isinstance(policy, JwtPolicySet)
        evil_rule = policy.rules[0].model_copy(update=update)
        evil = snap.model_copy(
            update={"policy_sets": (policy.model_copy(update={"rules": (evil_rule,)}),)}
        )
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)


class TestCertModel:
    def test_identity(self) -> None:
        assert CertIdentity(value="alice@example.test", user="alice").by == "cn"
        assert CertIdentity(by="sha1", value="a" * 40, user="robot")
        for bad in ({"by": "sha1", "value": "A" * 40}, {"by": "sha1", "value": "abc"}):
            with pytest.raises(ValidationError):
                CertIdentity(user="x", **bad)
        for bad_name in ("a b", "a|b", "a,b", "a\nb"):
            with pytest.raises(ValidationError):
                CertIdentity(value=bad_name, user="x")
            with pytest.raises(ValidationError):
                CertIdentity(value="x", user=bad_name)

    def test_rule_groups_go_with_the_groups_mode(self) -> None:
        assert ClientCertRule(mode="groups", groups=["ops"]).mode == "groups"
        for bad in ({"mode": "groups"}, {"mode": "valid", "groups": ["ops"]}):
            with pytest.raises(ValidationError):
                ClientCertRule(**bad)

    def test_set_rules(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            cert_set_model(identities=[{"value": "a", "user": "a"}] * 2)
        with pytest.raises(ValidationError, match="differ"):
            cert_set_model(user_header="X-A", groups_header="x-a")
        with pytest.raises(ValidationError, match="cannot carry identity"):
            cert_set_model(user_header="X-Forwarded-For")
        with pytest.raises(ValidationError, match="need identities"):
            cert_set_model(identities=[], rules=[{"mode": "identity"}])
        assert cert_set_model(identities=[], rules=[{"mode": "valid"}], user_header=None)


def cert_set_model(**extra: Any) -> Any:
    body = cert_set(**extra)
    body["id"], body["tenant_id"] = uuid.uuid4(), uuid.uuid4()
    return ClientCertPolicySet.model_validate(body)


class TestCertSnapshotAndRender:
    def test_valid_and_needs_a_client_ca(self) -> None:
        assert codes(data_with(cert_set(), client_ca=True)) == []
        assert codes(data_with(cert_set())) == ["client_cert_needs_client_ca"]

    def test_one_set_per_service(self) -> None:
        second = cert_set(id="ps-cc2", name="cc2", user_header="X-U2", groups_header="X-G2")
        assert codes(data_with(cert_set(), second, client_ca=True)) == ["multiple_client_cert_sets"]

    def test_identity_headers_must_not_collide(self) -> None:
        sub = jwt_set(rule(subject_header="X-Cert-User"))
        assert codes(data_with(cert_set(), sub, client_ca=True)) == ["identity_header_conflict"]

    def test_rules_maps_and_headers(self) -> None:
        body = cert_set(
            identities=[
                {"value": "alice@example.test", "user": "alice", "groups": ["ops", "staff"]},
                {"by": "sha1", "value": "0" * 40, "user": "robot"},
            ],
            rules=[
                {"match": {"path_prefix": "/ops"}, "mode": "groups", "groups": ["ops"]},
                {"match": {"path_prefix": "/x"}, "mode": "identity"},
                {"mode": "valid"},
            ],
        )
        data = data_with(body, client_ca=True)
        files = render_haproxy(snapshot_from_mapping(data), GROUP).files
        assert files["haproxy/maps/cc-certcn.map"] == "alice@example.test alice|ops,staff\n"
        assert files["haproxy/maps/cc-certsha1.map"] == f"{'0' * 40} robot|\n"
        lines = frontend(data)
        assert "http-request del-header X-Cert-User" in lines
        assert any(
            ln.startswith("http-request set-var(txn.sy_cid) ssl_c_sha1,hex,lower,map(")
            for ln in lines
        )
        assert (
            "http-request return status 403 if { path_beg /ops } !{ var(txn.sy_cdone) -m found } "
            "!{ var(txn.sy_cgrp) -m reg '(^|,)(ops)(,|$)' }" in lines
        )
        assert (
            "http-request return status 403 if !{ var(txn.sy_cdone) -m found } !{ ssl_c_used }"
            in lines
        )

    def test_a_valid_only_set_has_no_maps(self) -> None:
        data = data_with(cert_set(identities=[]), client_ca=True)
        files = render_haproxy(snapshot_from_mapping(data), GROUP).files
        assert not [p for p in files if "cert" in p and p.endswith(".map")]

    def test_hostile_identity_values_stop_the_render(self) -> None:
        snap = snapshot_from_mapping(data_with(cert_set(), client_ca=True))
        policy = snap.policy_sets[0]
        for update in ({"value": "a b"}, {"user": "x|y"}, {"groups": ("g,h",)}):
            assert isinstance(policy, ClientCertPolicySet)
            evil_id = policy.identities[0].model_copy(update=update)
            evil = snap.model_copy(
                update={"policy_sets": (policy.model_copy(update={"identities": (evil_id,)}),)}
            )
            with pytest.raises(RenderError):
                render_haproxy(evil, GROUP)


class TestJwks:
    def test_rsa_and_ec_keys_convert(self) -> None:
        rsa = RSAKey.generate_key(2048, parameters={"kid": "r1"}).as_dict(private=False)
        ec = ECKey.generate_key("P-256", parameters={"kid": "e1"}).as_dict(private=False)
        keys = jwks_to_keys(json.dumps({"keys": [rsa, ec]}))
        assert [(k["kid"], k["alg"]) for k in keys] == [("r1", "RS256"), ("e1", "ES256")]
        assert all(k["public_key_pem"].startswith("-----BEGIN PUBLIC KEY-----") for k in keys)
        JwtKey(**keys[0])

    def test_the_converted_key_is_the_originals_public_key(self) -> None:
        original = RSAKey.generate_key(2048)
        converted = jwk_to_key(original.as_dict(private=False))["public_key_pem"]
        assert converted.strip() == original.as_pem(private=False).decode().strip()

    @pytest.mark.parametrize(
        ("doc", "message"),
        [
            ("not json", "not JSON"),
            ("{}", "no keys"),
            ('{"keys": []}', "no keys"),
            ('{"keys": [{"kty": "oct", "k": "AAAA"}]}', "not supported"),
            (
                '{"keys": [{"kty": "RSA", "use": "enc", "n": "AQAB", "e": "AQAB"}]}',
                "no signing keys",
            ),
            ('{"keys": [{"kty": "EC", "crv": "secp256k1", "x": "AQ", "y": "AQ"}]}', "curve"),
            (
                '{"keys": [{"kty": "EC", "crv": "P-256", "alg": "ES384", "x": "AQ", "y": "AQ"}]}',
                "fit",
            ),
            ('{"keys": [{"kty": "RSA", "alg": "ES256", "n": "AQAB", "e": "AQAB"}]}', "fit"),
            ('{"keys": [{"kty": "RSA", "n": "AQAB"}]}', "e is missing"),
        ],
    )
    def test_refusals(self, doc: str, message: str) -> None:
        with pytest.raises(JwksError, match=message):
            jwks_to_keys(doc)

    def test_private_parameters_are_refused(self) -> None:
        private = RSAKey.generate_key(2048).as_dict(private=True)
        with pytest.raises(JwksError, match="private"):
            jwks_to_keys(json.dumps({"keys": [private]}))

    def test_more_than_eight_keys_are_refused(self) -> None:
        many = [RSAKey.generate_key(2048).as_dict(private=False) for _ in range(9)]
        with pytest.raises(JwksError, match="at most 8"):
            jwks_to_keys(json.dumps({"keys": many}))

    def test_the_command(self, tmp_path: Path) -> None:
        key = RSAKey.generate_key(2048, parameters={"kid": "r1"}).as_dict(private=False)
        path = tmp_path / "jwks.json"
        path.write_text(json.dumps({"keys": [key]}), "utf-8")
        result = CliRunner().invoke(app, ["jwks", "convert", str(path)])
        assert result.exit_code == 0 and json.loads(result.output)[0]["kid"] == "r1"
        path.write_text("{}", "utf-8")
        assert CliRunner().invoke(app, ["jwks", "convert", str(path)]).exit_code == 2

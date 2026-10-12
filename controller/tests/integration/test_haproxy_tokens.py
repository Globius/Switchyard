# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Real requests: JWT bearer validation (ADR 0135) and client-certificate identity (ADR 0136).

Same lab as ``test_haproxy_policies``: the edge at 192.0.2.20, backend A that echoes what it
received, clients 192.0.2.98 and .99. Tokens and certificates are made here and handed to the edge
the way the agent bundle does (``certs/`` files), so the rendered config and the files are exactly
what an engine gets.
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from joserfc import jwt
from joserfc.jwk import RSAKey

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.render.haproxy import render_haproxy
from switchyard.render.tokens import jwt_key_files
from tests.haproxy_check import HAPROXY_IMAGE
from tests.integration.test_haproxy_behavior import EDGE, EDGE_START, resolve
from tests.lab import BACKEND_IMAGE, SUBNET, Lab

ECHO_CFG = """\
global
    maxconn 100
defaults
    mode http
    timeout client 5s
    timeout server 5s
    timeout connect 5s
frontend f
    bind :8080
    http-request return status 200 content-type text/plain lf-string "be=A path=%[path] sub=%[req.hdr(x-jwt-sub)] cu=%[req.hdr(x-cert-user)] cg=%[req.fhdr(x-cert-groups)] auth=%[req.hdr(authorization),length]"
"""

ISSUER = "https://login.example.test/realms/acme"
AUDIENCE = "https://api.example.test/"


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


class Authority:
    """A throw-away client CA that signs leaf certificates."""

    def __init__(self, cn: str) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(_name(cn))
            .issuer_name(_name(cn))
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self.key, hashes.SHA256())
        )

    @property
    def pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode()

    def leaf(self, cn: str) -> tuple[str, str]:
        """``(certificate + key PEM, sha1 fingerprint hex)`` of a client certificate."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name(cn))
            .issuer_name(self.cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .sign(self.key, hashes.SHA256())
        )
        pem = (
            cert.public_bytes(serialization.Encoding.PEM).decode()
            + key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode()
        )
        return pem, cert.fingerprint(hashes.SHA1()).hex()  # noqa: S303


STAFF = Authority("staff-ca")
OTHER = Authority("other-ca")
ALICE, _ = STAFF.leaf("alice@example.test")
BOB, _ = STAFF.leaf("bob@example.test")
ROBOT, ROBOT_SHA1 = STAFF.leaf("robot-1")
MALLORY, _ = OTHER.leaf("alice@example.test")

SIGNING_KEY = RSAKey.generate_key(2048)
ROTATED_KEY = RSAKey.generate_key(2048)
STRANGER_KEY = RSAKey.generate_key(2048)


def token(key: RSAKey = SIGNING_KEY, kid: str = "k1", **claims: Any) -> str:
    now = int(time.time())
    body: dict[str, Any] = {
        "iss": ISSUER,
        "aud": [AUDIENCE, "other"],
        "sub": "alice",
        "scope": "read write",
        "exp": now + 600,
    }
    body.update(claims)
    return jwt.encode({"alg": "RS256", "kid": kid}, body, key)


def public_pem(key: RSAKey) -> str:
    return key.as_pem(private=False).decode()


def data() -> dict[str, Any]:
    def service(vid: str, port: int, **extra: Any) -> dict[str, Any]:
        return {
            "id": vid,
            "tenant_id": "t",
            "se_group_id": "g",
            "name": vid,
            "protocol": "http",
            "vip": EDGE,
            "ports": [{"port": port, "tls": True}],
            "certificate_ids": ["c"],
            "default_pool_id": "a-pool",
            **extra,
        }

    return {
        "tenants": [{"id": "t", "name": "acme"}],
        "networks": [{"id": "n", "tenant_id": "t", "name": "lab", "cidr": SUBNET}],
        "se_groups": [
            {
                "id": "g",
                "tenant_id": "t",
                "name": "edge",
                "ha_mode": "vrrp",
                "networks": [{"network_id": "n", "role": "vip"}],
            }
        ],
        "certificates": [
            {
                "id": "c",
                "tenant_id": "t",
                "name": "lab",
                "source": "upload",
                "sans": ["*.example.test", "app.example.test"],
                "not_after": "2038-01-01T00:00:00+00:00",
                "secret_ref": "kv/lab",
            },
            {
                "id": "ca",
                "tenant_id": "t",
                "name": "staff-ca",
                "source": "upload",
                "purpose": "client_ca",
                "sans": [],
                "not_after": "2038-01-01T00:00:00+00:00",
                "secret_ref": "kv/lab-ca",
            },
        ],
        "pools": [
            {
                "id": "a-pool",
                "tenant_id": "t",
                "name": "a-pool",
                "members": [{"address": "192.0.2.51", "port": 8080}],
            }
        ],
        "policy_sets": [
            {
                "id": "tok",
                "tenant_id": "t",
                "name": "tok",
                "kind": "jwt",
                "rules": [
                    {
                        "match": {"path_prefix": "/v1/admin"},
                        "issuer": ISSUER,
                        "audiences": [AUDIENCE],
                        "required_scopes": ["write"],
                        "subject_header": "X-Jwt-Sub",
                        "keys": [
                            {"kid": "k1", "public_key_pem": public_pem(SIGNING_KEY)},
                            {"kid": "k2", "public_key_pem": public_pem(ROTATED_KEY)},
                        ],
                    },
                    {
                        "match": {"path_prefix": "/v1"},
                        "issuer": ISSUER,
                        "keys": [{"public_key_pem": public_pem(SIGNING_KEY)}],
                        "leeway_s": 0,
                    },
                ],
            },
            {
                "id": "cc",
                "tenant_id": "t",
                "name": "cc",
                "kind": "client_cert",
                "identities": [
                    {"value": "alice@example.test", "user": "alice", "groups": ["staff", "ops"]},
                    {"value": "bob@example.test", "user": "bob", "groups": ["staff"]},
                    {"by": "sha1", "value": ROBOT_SHA1, "user": "robot", "groups": ["ops"]},
                ],
                "rules": [
                    {"match": {"path_prefix": "/ops"}, "mode": "groups", "groups": ["ops"]},
                    {"match": {"path_prefix": "/staff"}, "mode": "identity"},
                    {"match": {"path_prefix": "/any"}, "mode": "valid"},
                ],
            },
        ],
        "virtual_services": [
            service("api", 5443, policy_set_ids=["tok"]),
            service(
                "certs",
                5444,
                policy_set_ids=["cc"],
                ports=[
                    {"port": 5444, "tls": True, "client_ca_id": "ca", "client_verify": "optional"}
                ],
            ),
        ],
    }


@pytest.fixture(scope="module")
def lab(lab_network: Lab) -> Iterator[Lab]:
    with lab_network.scope():
        lab_network._run(
            BACKEND_IMAGE, "192.0.2.51", ["haproxy", "-f", "/be.cfg"], {"be.cfg": ECHO_CFG}
        )
        snapshot = snapshot_from_mapping(data())
        group = UUID(symbolic_uuid("g"))
        files = {f"etc/switchyard/{p}": c for p, c in render_haproxy(snapshot, group).files.items()}
        files |= {f"etc/switchyard/{p}": c for p, c in jwt_key_files(snapshot, group).items()}
        files["etc/switchyard/certs/ca-staff-ca.pem"] = STAFF.pem
        lab_network._run(HAPROXY_IMAGE, EDGE, ["sh", "-c", EDGE_START], files, user="root")
        yield lab_network


def _get(label: str, port: int, path: str, *args: str) -> str:
    host = "app.example.test"
    return f"req {label} {resolve(host, port)} {' '.join(args)} https://{host}:{port}{path}"


def _write(name: str, pem: str) -> str:
    return f"cat > /tmp/{name}.pem <<'PEM'\n{pem}PEM\n"


@pytest.fixture(scope="module")
def jwt_results(lab: Lab) -> dict[str, str]:
    def bearer(t: str) -> str:
        return f"-H 'Authorization: Bearer {t}'"

    cases = {
        "good": token(),
        "good-k2": token(ROTATED_KEY, "k2"),
        "expired": token(exp=int(time.time()) - 600),
        "slightly-expired": token(exp=int(time.time()) - 5),
        "iss": token(iss="https://evil.test/realms/acme"),
        "aud": token(aud="somebody-else"),
        "aud-string": token(aud=AUDIENCE),
        "scope": token(scope="read"),
        "sig": token(STRANGER_KEY, "k1"),
        "kid": token(kid="unknown"),
        "k2-key-as-k1": token(ROTATED_KEY, "k1"),
    }
    lines = [_get(f"admin-{n}", 5443, "/v1/admin/x", bearer(t)) for n, t in cases.items()]
    lines += [_get(f"v1-{n}", 5443, "/v1/x", bearer(t)) for n, t in cases.items()]
    lines += [
        _get("admin-none", 5443, "/v1/admin/x"),
        _get("open", 5443, "/open"),
        _get("garbage", 5443, "/v1/x", "-H 'Authorization: Bearer not.a.jwt'"),
        _get("basic", 5443, "/v1/x", "-H 'Authorization: Basic YTpi'"),
        _get("spoof", 5443, "/open", "-H 'X-Jwt-Sub: root'"),
        _get("spoof-admin", 5443, "/v1/admin/x", "-H 'X-Jwt-Sub: root'", bearer(cases["good"])),
    ]
    return lab.curl("192.0.2.99", chr(10).join(lines))


class TestJwt:
    @pytest.mark.parametrize("name", ["good", "good-k2", "aud-string"])
    def test_valid_tokens_reach_the_origin_with_the_subject(
        self, jwt_results: dict[str, str], name: str
    ) -> None:
        out = jwt_results[f"admin-{name}"]
        assert out.startswith("be=A ") and " sub=alice " in out and out.endswith("|200|"), out

    @pytest.mark.parametrize(
        "name", ["expired", "iss", "aud", "scope", "sig", "kid", "k2-key-as-k1"]
    )
    def test_bad_tokens_are_refused(self, jwt_results: dict[str, str], name: str) -> None:
        assert jwt_results[f"admin-{name}"] == "|401|"

    def test_the_second_rule_has_no_audience_or_scope_demands_and_no_leeway(
        self, jwt_results: dict[str, str]
    ) -> None:
        assert jwt_results["v1-good"].startswith("be=A ")
        assert jwt_results["v1-aud"].startswith("be=A ")
        assert jwt_results["v1-scope"].startswith("be=A ")
        assert jwt_results["v1-slightly-expired"] == "|401|", "leeway 0 refuses a token 5 s late"
        assert jwt_results["admin-slightly-expired"].startswith("be=A "), "default leeway is 30 s"

    def test_missing_malformed_and_other_credentials_are_refused(
        self, jwt_results: dict[str, str]
    ) -> None:
        for name in ("admin-none", "garbage", "basic"):
            assert jwt_results[name] == "|401|"

    def test_paths_without_a_rule_need_no_token(self, jwt_results: dict[str, str]) -> None:
        assert jwt_results["open"].startswith("be=A ")

    def test_a_client_supplied_subject_header_never_reaches_the_origin(
        self, jwt_results: dict[str, str]
    ) -> None:
        assert " sub= " in jwt_results["open"]
        assert " sub= " in jwt_results["spoof"]
        assert " sub=alice " in jwt_results["spoof-admin"]


@pytest.fixture(scope="module")
def cert_results(lab: Lab) -> dict[str, str]:
    def with_cert(name: str, *paths: tuple[str, str]) -> str:
        return f"--cert /tmp/{name}.pem"

    script = [
        _write("alice", ALICE),
        _write("bob", BOB),
        _write("robot", ROBOT),
        _write("mallory", MALLORY),
    ]
    for who in ("alice", "bob", "robot"):
        for path in ("/ops", "/staff", "/any", "/free"):
            script.append(_get(f"{who}{path}", 5444, path, with_cert(who)))
    script += [
        _get("none/ops", 5444, "/ops"),
        _get("none/staff", 5444, "/staff"),
        _get("none/any", 5444, "/any"),
        _get("none/free", 5444, "/free", "-H 'X-Cert-User: root' -H 'X-Cert-Groups: ops'"),
        _get("bob/free-spoof", 5444, "/free", with_cert("bob"), "-H 'X-Cert-User: root'"),
        _get("mallory/free", 5444, "/free", with_cert("mallory")),
    ]
    return lab.curl("192.0.2.99", chr(10).join(script))


class TestClientCertificates:
    def test_group_rule(self, cert_results: dict[str, str]) -> None:
        assert cert_results["alice/ops"].startswith("be=A ")
        assert " cu=alice cg=staff,ops " in cert_results["alice/ops"]
        assert cert_results["bob/ops"] == "|403|"
        assert cert_results["robot/ops"].startswith("be=A "), (
            "a fingerprint identity has groups too"
        )
        assert " cu=robot " in cert_results["robot/ops"]

    def test_identity_rule(self, cert_results: dict[str, str]) -> None:
        assert cert_results["bob/staff"].startswith("be=A ")
        assert cert_results["robot/staff"].startswith("be=A ")

    def test_valid_rule_accepts_any_certificate_of_the_client_ca_and_refuses_none(
        self, cert_results: dict[str, str]
    ) -> None:
        assert cert_results["bob/any"].startswith("be=A ")
        assert cert_results["none/any"] == "|403|"
        assert cert_results["none/ops"] == "|403|"
        assert cert_results["none/staff"] == "|403|"

    def test_unruled_paths_need_no_certificate_and_identity_headers_are_never_client_supplied(
        self, cert_results: dict[str, str]
    ) -> None:
        assert " cu= cg= " in cert_results["none/free"]
        assert " cu=bob " in cert_results["bob/free-spoof"]

    def test_a_certificate_from_another_ca_fails_the_handshake(
        self, cert_results: dict[str, str]
    ) -> None:
        assert cert_results["mallory/free"].startswith("FAIL(")

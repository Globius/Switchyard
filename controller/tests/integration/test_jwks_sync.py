"""JWT rules with a ``jwks_url``: the controller keeps their keys in step with the issuer (ADR 0153)."""

import base64
import json
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from switchyard.domain import models as dm
from switchyard.domain.loader import symbolic_uuid
from switchyard.domain.workflow import ChangeSetState
from switchyard.services.jwks import JwksError
from switchyard.services.jwks_sync import JwksSync
from tests.pipeline_env import TENANT, World, principal, world, world_data

pytestmark = pytest.mark.usefixtures("owner_engine")

POLICY = UUID(symbolic_uuid("ps-jwt"))
URL = "https://login.example.test/.well-known/jwks.json"
PEM_EXAMPLE = "-----BEGIN PUBLIC KEY-----\n" + "A" * 64 + "\n-----END PUBLIC KEY-----\n"


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwks(kid: str) -> str:
    """A JWKS with one fresh RSA public key."""
    numbers = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_numbers()
    )
    return json.dumps(
        {
            "keys": [
                {"kty": "RSA", "kid": kid, "use": "sig", "n": _b64(numbers.n), "e": _b64(numbers.e)}
            ]
        }
    )


def data(*, keys: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    d = world_data()
    d["policy_sets"] = [
        {
            "id": "ps-jwt",
            "tenant_id": "t-acme",
            "name": "api-tokens",
            "kind": "jwt",
            "rules": [
                {
                    "issuer": "https://login.example.test",
                    "jwks_url": URL,
                    "keys": keys or [],
                }
            ],
        }
    ]
    return d


class Issuer:
    """A stand-in for the issuer's JWKS endpoint."""

    def __init__(self, text: str | None) -> None:
        self.text = text
        self.calls = 0

    async def __call__(self, url: str) -> str:
        self.calls += 1
        if self.text is None:
            raise JwksError("unreachable")
        return self.text


async def sync(w: World, issuer: Issuer) -> int:
    changed = await JwksSync(w.db, w.service, issuer).run_tenant(TENANT)
    for record in await w.service.list_change_sets(
        principal(), TENANT, states={ChangeSetState.APPLYING}
    ):
        await w.pipeline.run(TENANT, record.id)
    return changed


async def keys(w: World) -> list[tuple[str | None, str]]:
    policy = next(p for p in (await w.snapshot()).policy_sets if p.id == POLICY)
    assert isinstance(policy, dm.JwtPolicySet)
    return [(k.kid, k.alg) for k in policy.rules[0].keys]


def test_a_rule_needs_keys_or_a_jwks_url() -> None:
    with pytest.raises(ValueError, match="keys or a jwks_url"):
        dm.JwtRule(issuer="https://login.example.test")
    assert dm.JwtRule(issuer="https://login.example.test", jwks_url=URL).keys == ()
    with pytest.raises(ValueError, match="jwks_url must be an https URL"):
        dm.JwtRule(issuer="https://login.example.test", jwks_url="http://login.example.test/jwks")


async def test_the_first_fetch_fills_the_keys_and_the_next_changes_nothing(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        issuer = Issuer(jwks("k1"))
        assert await keys(w) == []
        assert await sync(w, issuer) == 1
        assert await keys(w) == [("k1", "RS256")]
        same = Issuer(issuer.text)
        assert await sync(w, same) == 0, "the same keys do not open a change set"


async def test_a_rotation_replaces_the_keys(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        assert await sync(w, Issuer(jwks("old"))) == 1
        assert await sync(w, Issuer(jwks("new"))) == 1
        assert await keys(w) == [("new", "RS256")]


async def test_an_unreachable_or_empty_issuer_keeps_the_keys(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        assert await sync(w, Issuer(jwks("k1"))) == 1
        assert await sync(w, Issuer(None)) == 0
        assert await sync(w, Issuer('{"keys": []}')) == 0
        assert await sync(w, Issuer("<html>")) == 0
        assert await keys(w) == [("k1", "RS256")]

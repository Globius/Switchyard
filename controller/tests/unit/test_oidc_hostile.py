"""ID-token validation against hostile tokens (docs/security/backend-review.md)."""

import json
import time
from typing import Any

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, OctKey, RSAKey

from switchyard.services.oidc import OidcClient, OidcError, ProviderConfig

ISSUER = "https://idp.example.test"
CLIENT = "switchyard"
NONCE = "n-123"
KEY = RSAKey.generate_key(2048, {"kid": "k1"})
OTHER = RSAKey.generate_key(2048, {"kid": "k1"})


def cfg() -> ProviderConfig:
    return ProviderConfig(
        "1", "corp", ISSUER, CLIENT, None, "openid", "preferred_username", "email", "groups"
    )


def client() -> OidcClient:
    jwks = KeySet([KEY]).as_dict(private=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks)

    return OidcClient(transport=httpx.MockTransport(handler))


def claims(**over: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "aud": CLIENT,
        "sub": "u-1",
        "exp": now + 300,
        "iat": now,
        "nonce": NONCE,
    }
    return {k: v for k, v in (base | over).items() if v is not None}


async def check(token: str) -> dict[str, Any]:
    return await client()._validate(cfg(), token, "https://idp.example.test/jwks", NONCE)


def b64(data: dict[str, Any]) -> str:
    import base64

    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


async def test_a_good_token_is_accepted() -> None:
    token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims(), KEY)
    assert (await check(token))["sub"] == "u-1"


@pytest.mark.parametrize(
    "over",
    [
        {"iss": "https://evil.example.test"},
        {"aud": "someone-else"},
        {"aud": ["someone-else", "another"]},
        {"exp": int(time.time()) - 3600},
        {"nonce": "replayed"},
        {"nonce": None},
        {"sub": None},
        {"exp": None},
    ],
)
async def test_bad_claims_are_refused(over: dict[str, Any]) -> None:
    token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims(**over), KEY)
    with pytest.raises(OidcError):
        await check(token)


async def test_a_token_signed_by_another_key_is_refused() -> None:
    token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims(), OTHER)
    with pytest.raises(OidcError):
        await check(token)


async def test_alg_none_is_refused() -> None:
    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims())}."
    with pytest.raises(OidcError):
        await check(token)


async def test_hmac_with_the_public_key_as_the_secret_is_refused() -> None:
    public_pem = KEY.as_pem(private=False)
    forged = jwt.encode({"alg": "HS256", "kid": "k1"}, claims(), OctKey.import_key(public_pem))
    with pytest.raises(OidcError):
        await check(forged)


@pytest.mark.parametrize("junk", ["", "a.b", "a.b.c.d", "not a jwt", "e30.e30.e30"])
async def test_garbage_is_refused(junk: str) -> None:
    with pytest.raises(OidcError):
        await check(junk)

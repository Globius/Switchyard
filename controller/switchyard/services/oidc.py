"""OpenID Connect authorization-code login with PKCE.

Only what the controller needs: discovery, the authorization URL, the code exchange and ID-token
validation (signature through the provider's JWKS, issuer, audience, expiry and nonce). The token
endpoint answer is never trusted for anything but the ID token; groups and names come from its
claims. Built on ``httpx`` and ``joserfc``.
"""

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "PS384", "PS512"]
CACHE_SECONDS = 300.0


class OidcError(Exception):
    """Login with an identity provider failed. The message is safe to show to the user."""


@dataclass(frozen=True)
class ProviderConfig:
    """An identity provider as configured by an administrator."""

    id: str
    name: str
    issuer_url: str
    client_id: str
    client_secret: str | None
    scopes: str
    username_claim: str
    email_claim: str
    groups_claim: str


def new_pkce() -> tuple[str, str]:
    """Return ``(verifier, S256 challenge)``.

    Example:
        >>> verifier, challenge = new_pkce()
        >>> len(verifier) >= 43 and "=" not in challenge
        True
    """
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class OidcClient:
    """Talks to OpenID Connect providers; caches discovery documents and key sets briefly."""

    def __init__(
        self, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0
    ) -> None:
        """Create a client; ``transport`` is replaceable in tests."""
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout)
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _get_json(self, url: str) -> Any:
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError("the identity provider could not be reached") from exc
        self._cache[url] = (time.monotonic(), data)
        return data

    async def check_discovery(self, issuer: str) -> None:
        """Fetch the provider's discovery document afresh and check it names this issuer.

        Used when an administrator configures a provider; a failure is a warning, not a refusal.

        Raises:
            OidcError: If the document is unreachable or does not match the issuer.
        """
        self._cache.pop(issuer.rstrip("/") + "/.well-known/openid-configuration", None)
        await self._discovery(issuer)

    async def _discovery(self, issuer: str) -> dict[str, Any]:
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        doc = await self._get_json(url)
        if not isinstance(doc, dict) or str(doc.get("issuer", "")).rstrip("/") != issuer.rstrip(
            "/"
        ):
            raise OidcError("the provider's discovery document does not match its issuer")
        return doc

    async def authorization_url(
        self,
        cfg: ProviderConfig,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        """Build the URL the browser is sent to."""
        endpoint = (await self._discovery(cfg.issuer_url)).get("authorization_endpoint")
        if not isinstance(endpoint, str):
            raise OidcError("the provider has no authorization endpoint")
        query = urlencode(
            {
                "response_type": "code",
                "client_id": cfg.client_id,
                "redirect_uri": redirect_uri,
                "scope": cfg.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    async def exchange(
        self,
        cfg: ProviderConfig,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        nonce: str,
    ) -> dict[str, Any]:
        """Trade the code for tokens and return the validated ID-token claims."""
        doc = await self._discovery(cfg.issuer_url)
        token_endpoint, jwks_uri = doc.get("token_endpoint"), doc.get("jwks_uri")
        if not isinstance(token_endpoint, str) or not isinstance(jwks_uri, str):
            raise OidcError("the provider's discovery document is incomplete")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": cfg.client_id,
        }
        headers: dict[str, str] = {}
        if cfg.client_secret:
            pair = f"{quote_plus(cfg.client_id)}:{quote_plus(cfg.client_secret)}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        try:
            response = await self._http.post(token_endpoint, data=form, headers=headers)
        except httpx.HTTPError as exc:
            raise OidcError("the identity provider could not be reached") from exc
        if response.status_code != 200:
            raise OidcError("the identity provider refused the login code")
        id_token = response.json().get("id_token")
        if not isinstance(id_token, str):
            raise OidcError("the identity provider returned no ID token")
        return await self._validate(cfg, id_token, jwks_uri, nonce)

    async def _validate(
        self, cfg: ProviderConfig, id_token: str, jwks_uri: str, nonce: str
    ) -> dict[str, Any]:
        keys = KeySet.import_key_set(await self._get_json(jwks_uri))
        try:
            token = jwt.decode(id_token, keys, algorithms=ALLOWED_ALGORITHMS)
            registry = jwt.JWTClaimsRegistry(
                leeway=60,
                iss={"essential": True, "value": cfg.issuer_url.rstrip("/")},
                aud={"essential": True, "value": cfg.client_id},
                exp={"essential": True},
                sub={"essential": True},
                nonce={"essential": True, "value": nonce},
            )
            claims = dict(token.claims)
            if str(claims.get("iss", "")).rstrip("/") == cfg.issuer_url.rstrip("/"):
                claims["iss"] = cfg.issuer_url.rstrip("/")
            registry.validate(claims)
        except (JoseError, ValueError, KeyError) as exc:
            raise OidcError("the ID token from the identity provider is not valid") from exc
        return claims

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""OAuth 2.0 client-credentials tokens, cached until shortly before they expire.

Used for ACME providers that sit behind an identity provider (for example Keyfactor): the controller
signs in as a service, gets a bearer token and presents it to the provider's API.
"""

import time

import httpx


class OAuthError(Exception):
    """Getting a token failed."""


class ClientCredentials:
    """Fetches and caches access tokens with the client-credentials grant."""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        scope: str | None = None,
        audience: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: bool = True,
    ) -> None:
        """Create the token source.

        Args:
            token_url: The identity provider's token endpoint.
            client_id: The service's client id.
            client_secret: Its secret (kept in the secrets backend).
            scope: Optional ``scope`` form field.
            audience: Optional ``audience`` form field (Auth0 and some others).
            transport: Replaceable HTTP transport (tests).
            verify: TLS verification (``False`` only for test servers).
        """
        self._url = token_url
        self._form = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scope:
            self._form["scope"] = scope
        if audience:
            self._form["audience"] = audience
        self._http = httpx.AsyncClient(transport=transport, verify=verify, timeout=30.0)
        self._token: str | None = None
        self._expires = 0.0

    async def token(self) -> str:
        """A valid access token (a cached one while it has more than a minute left)."""
        if self._token and time.time() < self._expires - 60:
            return self._token
        try:
            response = await self._http.post(self._url, data=self._form)
        except httpx.HTTPError as exc:
            raise OAuthError(f"token request to {self._url}: {exc}") from exc
        if response.status_code != 200:
            raise OAuthError(
                f"token request to {self._url}: HTTP {response.status_code}: {_describe(response)}"
            )
        body = response.json()
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise OAuthError("the token endpoint answered without an access_token")
        self._token = token
        self._expires = time.time() + float(body.get("expires_in", 300))
        return token

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()


def _describe(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return str(body.get("error_description") or body.get("error") or body)[:300]

"""Secret storage adapter.

PostgreSQL, logs and the Git mirror never hold secrets; they hold ``secret_ref`` strings. This
adapter turns a reference into the secret. Three implementations ship:

- :class:`EnvSecrets`: reads ``SWITCHYARD_SECRET_<REF>`` variables. Useful for development
  and small deployments; the name is the reference upper-cased with non-alphanumerics replaced by
  ``_``.
- :class:`OpenBaoSecrets`: OpenBao KV v2 with AppRole login; the production backend. It also writes.
- :class:`MemorySecrets`: a dictionary, for tests.
"""

import os
import re
from collections.abc import Mapping
from typing import Protocol

import httpx


class SecretNotFoundError(LookupError):
    """No secret exists for a reference."""


class SecretBackendError(Exception):
    """The secrets backend could not be reached or refused the request."""


class SecretsWriter(Protocol):
    """Stores secrets (certificate keys, issued material)."""

    async def put(self, ref: str, value: str) -> None:
        """Store ``value`` under ``ref``; a new write becomes the value ``get`` returns."""
        ...


class SecretsReader(Protocol):
    """Resolves ``secret_ref`` strings to secret values."""

    async def get(self, ref: str) -> str:
        """Return the secret for ``ref``.

        Raises:
            SecretNotFoundError: If there is none.
        """
        ...


class MemorySecrets:
    """Secrets held in a dictionary."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        """Create the store with optional initial ``ref -> secret`` pairs."""
        self._values = dict(values or {})

    async def put(self, ref: str, value: str) -> None:
        """Store a secret."""
        self._values[ref] = value

    async def get(self, ref: str) -> str:
        """Return the secret for ``ref``."""
        try:
            return self._values[ref]
        except KeyError:
            raise SecretNotFoundError(ref) from None


class EnvSecrets:
    """Secrets read from environment variables.

    Example:
        >>> EnvSecrets.variable_name("kv/engines/se-1/password")
        'SWITCHYARD_SECRET_KV_ENGINES_SE_1_PASSWORD'
    """

    PREFIX = "SWITCHYARD_SECRET_"

    @classmethod
    def variable_name(cls, ref: str) -> str:
        """Return the environment variable that holds ``ref``."""
        return cls.PREFIX + re.sub(r"[^A-Za-z0-9]", "_", ref).upper()

    async def get(self, ref: str) -> str:
        """Return the secret for ``ref`` from the environment."""
        value = os.environ.get(self.variable_name(ref))
        if value is None:
            raise SecretNotFoundError(ref)
        return value


class OpenBaoSecrets:
    """OpenBao (or Vault-compatible) KV v2 over HTTPS with AppRole authentication.

    A reference is ``<mount>/<path>``: ``kv/tenants/acme/certs/web`` reads
    ``/v1/kv/data/tenants/acme/certs/web`` and the value lives under the key ``value``. The token
    is fetched lazily and renewed by logging in again when OpenBao answers 403.

    Example:
        >>> OpenBaoSecrets.split("kv/tenants/acme/certs/web")
        ('kv', 'tenants/acme/certs/web')
    """

    def __init__(
        self,
        address: str,
        role_id: str,
        secret_id: str,
        *,
        verify: bool | str = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create a client for the OpenBao server at ``address`` (``https://bao:8200``)."""
        self._http = httpx.AsyncClient(
            base_url=address.rstrip("/"), verify=verify, transport=transport, timeout=15.0
        )
        self._role = (role_id, secret_id)
        self._token: str | None = None

    @staticmethod
    def split(ref: str) -> tuple[str, str]:
        """Split a reference into KV mount and path."""
        mount, _, path = ref.partition("/")
        if not mount or not path:
            raise SecretNotFoundError(ref)
        return mount, path

    async def _login(self) -> str:
        response = await self._http.post(
            "/v1/auth/approle/login", json={"role_id": self._role[0], "secret_id": self._role[1]}
        )
        if response.status_code >= 400:
            raise SecretBackendError(f"OpenBao login failed: HTTP {response.status_code}")
        self._token = str(response.json()["auth"]["client_token"])
        return self._token

    async def _request(
        self, method: str, url: str, body: dict[str, object] | None = None
    ) -> httpx.Response:
        response: httpx.Response | None = None
        for _attempt in range(2):
            token = self._token or await self._login()
            try:
                response = await self._http.request(
                    method, url, headers={"X-Vault-Token": token}, json=body
                )
            except httpx.HTTPError as exc:
                raise SecretBackendError(f"OpenBao request failed: {exc}") from exc
            if response.status_code != 403:
                break
            self._token = None  # expired or revoked: log in again once
        assert response is not None  # noqa: S101
        return response

    async def get(self, ref: str) -> str:
        """Return the secret stored at ``ref``."""
        mount, path = self.split(ref)
        response = await self._request("GET", f"/v1/{mount}/data/{path}")
        if response.status_code == 404:
            raise SecretNotFoundError(ref)
        if response.status_code >= 400:
            raise SecretBackendError(f"OpenBao read of {ref}: HTTP {response.status_code}")
        value = response.json()["data"]["data"].get("value")
        if not isinstance(value, str):
            raise SecretNotFoundError(ref)
        return value

    async def put(self, ref: str, value: str) -> None:
        """Write a new version of the secret at ``ref``."""
        mount, path = self.split(ref)
        response = await self._request(
            "POST", f"/v1/{mount}/data/{path}", {"data": {"value": value}}
        )
        if response.status_code >= 400:
            raise SecretBackendError(f"OpenBao write of {ref}: HTTP {response.status_code}")

    async def call(self, path: str, body: dict[str, object]) -> dict[str, object]:
        """POST ``body`` to an OpenBao API path such as ``pki/sign/agents``; return ``data``."""
        response = await self._request("POST", f"/v1/{path.lstrip('/')}", body)
        if response.status_code >= 400:
            raise SecretBackendError(f"OpenBao call to {path}: HTTP {response.status_code}")
        data = response.json().get("data")
        return data if isinstance(data, dict) else {}

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()

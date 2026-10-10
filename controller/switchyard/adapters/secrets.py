"""Secret storage adapter.

PostgreSQL, logs and the Git mirror never hold secrets; they hold ``secret_ref`` strings. This
adapter turns a reference into the secret. Two implementations ship:

- :class:`EnvSecrets`: reads ``SWITCHYARD_SECRET_<REF>`` variables. Useful for development
  and small deployments; the name is the reference upper-cased with non-alphanumerics replaced by
  ``_``.
- :class:`MemorySecrets`: a dictionary, for tests.

The OpenBao KV v2 implementation is added with the certificate work in M5, behind the same
interface.
"""

import os
import re
from collections.abc import Mapping
from typing import Protocol


class SecretNotFoundError(LookupError):
    """No secret exists for a reference."""


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

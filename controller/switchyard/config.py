"""Process configuration read from environment variables."""

import os

#: Default points at ``deploy/dev/compose.yaml``.
DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/switchyard"


def database_url() -> str:
    """Return the SQLAlchemy URL from ``SWITCHYARD_DATABASE_URL`` or the dev default.

    Example:
        >>> database_url().startswith("postgresql+psycopg://")
        True
    """
    return os.environ.get("SWITCHYARD_DATABASE_URL", DEFAULT_DATABASE_URL)

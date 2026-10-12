# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Process configuration read from environment variables."""

import os

#: Default points at ``deploy/dev/compose.yaml``.
#: Public base URL of the login broker (ADR 0137), for example ``https://auth.example.test/dex``.
BROKER_URL_ENV = "SWITCHYARD_AUTH_BROKER_URL"
DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/switchyard"


def database_url() -> str:
    """Return the SQLAlchemy URL from ``SWITCHYARD_DATABASE_URL`` or the dev default.

    Example:
        >>> database_url().startswith("postgresql+psycopg://")
        True
    """
    return os.environ.get("SWITCHYARD_DATABASE_URL", DEFAULT_DATABASE_URL)

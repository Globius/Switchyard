# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Process configuration read from environment variables."""

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Default points at ``deploy/dev/compose.yaml``.
#: Public base URL of the login broker (ADR 0137), for example ``https://auth.example.test/dex``.
BROKER_URL_ENV = "SWITCHYARD_AUTH_BROKER_URL"
DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/switchyard"


def broker_url() -> str | None:
    """Return the login broker's public base URL (``SWITCHYARD_AUTH_BROKER_URL``) or ``None``.

    Example:
        >>> broker_url() is None or broker_url().startswith("http")
        True
    """
    return os.environ.get(BROKER_URL_ENV) or None


def database_url() -> str:
    """Return the SQLAlchemy URL from ``SWITCHYARD_DATABASE_URL`` or the dev default.

    Example:
        >>> database_url().startswith("postgresql+psycopg://")
        True
    """
    return os.environ.get("SWITCHYARD_DATABASE_URL", DEFAULT_DATABASE_URL)


def normalize_database_url(url: str) -> str:
    """Turn a multi-host URL into the form SQLAlchemy and psycopg both understand.

    The production Compose lists every database node (``@10.0.0.11:5432,10.0.0.12:5432/db``),
    which libpq accepts but SQLAlchemy's URL parser does not (it fails on the comma). The hosts
    move to the ``host`` and ``port`` query parameters, which pass through to libpq unchanged. A
    URL with one host is returned as it is.

    Example:
        >>> normalize_database_url("postgresql+psycopg://u:p@a:5432,b:5433/db")
        'postgresql+psycopg://u:p@/db?host=a%2Cb&port=5432%2C5433'
        >>> normalize_database_url("postgresql+psycopg://u:p@a:5432/db")
        'postgresql+psycopg://u:p@a:5432/db'
    """
    parts = urlsplit(url)
    userinfo, at, hostlist = parts.netloc.rpartition("@")
    if "," not in hostlist:
        return url
    hosts: list[str] = []
    ports: list[str] = []
    for entry in (e.strip() for e in hostlist.split(",")):
        if entry.startswith("["):  # [IPv6]:port
            host, _, rest = entry[1:].partition("]")
            port = rest.removeprefix(":")
        else:
            host, _, port = entry.partition(":")
        hosts.append(host)
        ports.append(port or "5432")
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [("host", ",".join(hosts)), ("port", ",".join(ports))]
    return urlunsplit((parts.scheme, userinfo + at, parts.path, urlencode(query), parts.fragment))

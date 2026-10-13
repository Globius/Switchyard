# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run Alembic migrations programmatically (used by tests, ``syctl db upgrade`` and deployments)."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from switchyard.config import normalize_database_url

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    """Return an Alembic config for ``url`` (a ``postgresql+psycopg`` SQLAlchemy URL)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", normalize_database_url(url).replace("%", "%%"))
    return cfg


#: Advisory-lock key that serializes migrations ("SYSMIG"). Three controller nodes start together
#: and each runs ``db upgrade``; without the lock they race on the first tables (found on the lab).
MIGRATION_LOCK_KEY = 0x535953_4D4947


@contextmanager
def _migration_lock(url: str) -> Iterator[None]:
    """Hold a PostgreSQL advisory lock while one process migrates the database."""
    engine = create_engine(normalize_database_url(url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
            try:
                yield
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
    finally:
        engine.dispose()


def upgrade(url: str, revision: str = "head") -> None:
    """Upgrade the database at ``url`` to ``revision`` (default: latest).

    Safe to run on several nodes at once: the second caller waits for the first and finds the
    schema already current.
    """
    with _migration_lock(url):
        command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    """Downgrade the database at ``url`` to ``revision``."""
    with _migration_lock(url):
        command.downgrade(alembic_config(url), revision)

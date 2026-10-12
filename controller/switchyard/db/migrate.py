# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run Alembic migrations programmatically (used by tests, ``syctl db upgrade`` and deployments)."""

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    """Return an Alembic config for ``url`` (a ``postgresql+psycopg`` SQLAlchemy URL)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    """Upgrade the database at ``url`` to ``revision`` (default: latest)."""
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    """Downgrade the database at ``url`` to ``revision``."""
    command.downgrade(alembic_config(url), revision)

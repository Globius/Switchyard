# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Alembic environment: synchronous psycopg connection, metadata from the ORM models."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from switchyard.db import models  # noqa: F401  (imports register the tables on Base.metadata)
from switchyard.db.base import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()

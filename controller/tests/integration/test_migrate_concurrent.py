# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Three controller nodes start together and each runs ``db upgrade`` (found on the lab)."""

import threading

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from switchyard.db import migrate


def test_concurrent_upgrades_serialize_instead_of_failing(pg_url: str) -> None:
    base = make_url(pg_url)
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS concurrent_migrate"))
        conn.execute(text("CREATE DATABASE concurrent_migrate"))
    url = base.set(database="concurrent_migrate").render_as_string(hide_password=False)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            migrate.upgrade(url)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    assert errors == []
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar() == 1
    engine.dispose()
    admin.dispose()

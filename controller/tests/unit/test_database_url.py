# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The production Compose lists every database node in one URL (found on the lab: migrate failed)."""

from sqlalchemy.engine import make_url

from switchyard.config import normalize_database_url
from switchyard.db.migrate import alembic_config


def test_a_multi_host_url_becomes_libpq_host_and_port_parameters() -> None:
    url = (
        "postgresql+psycopg://postgres:pw@172.30.0.11:5432,172.30.0.12:5432,172.30.0.13:5432"
        "/switchyard?target_session_attrs=read-write"
    )
    parsed = make_url(normalize_database_url(url))
    assert parsed.host is None and parsed.database == "switchyard"
    assert parsed.query["host"] == "172.30.0.11,172.30.0.12,172.30.0.13"
    assert parsed.query["port"] == "5432,5432,5432"
    assert parsed.query["target_session_attrs"] == "read-write"


def test_ports_ipv6_hosts_and_defaults_are_kept_per_host() -> None:
    parsed = make_url(normalize_database_url("postgresql+psycopg://u:p@[fd00::1]:5433,db2/x"))
    assert parsed.query["host"] == "fd00::1,db2"
    assert parsed.query["port"] == "5433,5432"


def test_a_single_host_is_left_alone() -> None:
    url = "postgresql+psycopg://u:p@db:5432/switchyard?sslmode=require"
    assert normalize_database_url(url) == url


def test_migrations_take_the_multi_host_form_too() -> None:
    cfg = alembic_config("postgresql+psycopg://u:p@a:5432,b:5432/db")
    assert "host=a%2Cb" in str(cfg.get_main_option("sqlalchemy.url"))

"""``syctl`` against a real PostgreSQL: seed, render, errors."""

import re
from pathlib import Path

from typer.testing import CliRunner

from switchyard.cli import app
from switchyard.render.haproxy import render_haproxy
from switchyard.services.seed import example_snapshot
from tests.haproxy_check import haproxy_check

runner = CliRunner()


def syctl(pg_url: str, *args: str) -> tuple[int, str, str]:
    result = runner.invoke(app, [*args, "--database-url", pg_url])
    return result.exit_code, result.stdout, result.stderr


def test_seed_then_render_prints_every_file(pg_url: str, owner_engine: object) -> None:
    code, out, _ = syctl(pg_url, "seed", "--example")
    assert code == 0, out
    assert "seeded tenant 'demo'" in out

    code, out, err = syctl(pg_url, "render", "--tenant", "demo", "--se-group", "edge")
    assert code == 0, err
    assert "==> haproxy.cfg <==" in out
    assert "==> crt-lists/web.crtlist <==" in out
    assert "==> maps/web.host.map <==" in out
    assert "frontend vs_web" in out
    assert re.search(r"content hash [0-9a-f]{64}", err)


def test_database_render_matches_direct_render(
    pg_url: str, owner_engine: object, tmp_path: Path
) -> None:
    assert syctl(pg_url, "seed", "--example")[0] == 0
    code, _, err = syctl(
        pg_url, "render", "--tenant", "demo", "--se-group", "edge", "--out", str(tmp_path)
    )
    assert code == 0, err
    snap = example_snapshot()
    direct = render_haproxy(snap, snap.se_groups[0].id)
    written = {
        p.relative_to(tmp_path).as_posix(): p.read_text("utf-8")
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert written == dict(direct.files)
    assert direct.content_hash in err
    assert haproxy_check(written).ok


def test_seed_twice_is_refused(pg_url: str, owner_engine: object) -> None:
    assert syctl(pg_url, "seed", "--example")[0] == 0
    code, _, err = syctl(pg_url, "seed", "--example")
    assert code == 1
    assert "already exists" in err


def test_render_unknown_tenant_and_group(pg_url: str, owner_engine: object) -> None:
    assert syctl(pg_url, "seed", "--example")[0] == 0
    code, _, err = syctl(pg_url, "render", "--tenant", "nope", "--se-group", "edge")
    assert (code, "tenant 'nope' not found" in err) == (1, True)
    code, _, err = syctl(pg_url, "render", "--tenant", "demo", "--se-group", "nope")
    assert (code, "SE group 'nope' not found" in err) == (1, True)


def test_seed_without_example_flag_is_an_error(pg_url: str) -> None:
    code, _, err = syctl(pg_url, "seed")
    assert code == 2
    assert "--example" in err


def test_db_upgrade_is_idempotent(pg_url: str) -> None:
    for _ in range(2):
        code, out, _ = syctl(pg_url, "db", "upgrade")
        assert code == 0
        assert "revision head" in out

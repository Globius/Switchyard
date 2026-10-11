"""Smoke tests for the ``syctl`` entry point."""

import pytest
from typer.testing import CliRunner

from switchyard import __version__
from switchyard.cli import app


def test_version_command_prints_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_grafana_url_is_taken_from_the_environment_and_only_if_it_is_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from switchyard import runtime

    monkeypatch.delenv("SWITCHYARD_GRAFANA_URL", raising=False)
    assert runtime.grafana_url() is None
    for good, shown in (
        ("https://grafana.example.test/", "https://grafana.example.test"),
        ("http://10.0.0.5:3000", "http://10.0.0.5:3000"),
    ):
        monkeypatch.setenv("SWITCHYARD_GRAFANA_URL", good)
        assert runtime.grafana_url() == shown
    for bad in ("javascript:alert(1)", "/grafana", "https://user:pw@grafana.example.test", ""):
        monkeypatch.setenv("SWITCHYARD_GRAFANA_URL", bad)
        assert runtime.grafana_url() is None

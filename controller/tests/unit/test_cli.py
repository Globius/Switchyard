"""Smoke tests for the ``syctl`` entry point."""

from typer.testing import CliRunner

from switchyard import __version__
from switchyard.cli import app


def test_version_command_prints_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__

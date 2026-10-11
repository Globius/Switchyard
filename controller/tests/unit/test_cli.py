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


def test_audit_syslog_is_off_by_default_and_parses_its_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging
    import socket

    from switchyard import runtime

    monkeypatch.delenv("SWITCHYARD_AUDIT_SYSLOG", raising=False)
    assert runtime.setup_audit_syslog() is False
    assert runtime.audit_syslog_target("tcp://10.0.0.9:6514") == (
        ("10.0.0.9", 6514),
        socket.SOCK_STREAM,
    )
    assert runtime.audit_syslog_target("udp://logs") == (("logs", 514), socket.SOCK_DGRAM)
    for bad in ("udp://", "file:///x", "logs:514"):
        with pytest.raises(ValueError, match="SWITCHYARD_AUDIT_SYSLOG"):
            runtime.audit_syslog_target(bad)
    monkeypatch.setenv("SWITCHYARD_AUDIT_SYSLOG", "udp://127.0.0.1:5514")
    audit = logging.getLogger("switchyard.audit")
    before = list(audit.handlers)
    try:
        assert runtime.setup_audit_syslog() is True
        assert len(audit.handlers) == len(before) + 1
    finally:
        for handler in audit.handlers[len(before) :]:
            audit.removeHandler(handler)
            handler.close()

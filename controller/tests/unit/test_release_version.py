# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""tools/release/version.py: nightly and release version numbers and stamping (ADR 0206)."""

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[3]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "sy_release_version", REPO / "tools/release/version.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


v = load()


def test_the_repository_is_consistent_with_its_version_file() -> None:
    assert v.check(REPO) == []


def test_nightly_release_and_candidate_numbers() -> None:
    assert v.compute("nightly", "0.2.0", date="20261014") == "0.2.0-nightly.20261014"
    assert v.compute("release", "0.2.0", tag="v0.2.0") == "0.2.0"
    assert v.compute("release", "0.2.0", tag="v0.2.0-rc1") == "0.2.0-rc1"
    assert v.compute("release", "0.2.0", tag="v0.2.0-rc.2") == "0.2.0-rc2"
    assert v.compute("dev", "0.2.0") == "0.2.0-dev"


@pytest.mark.parametrize(
    ("tag", "message"),
    [
        ("v0.3.0", "does not match VERSION"),
        ("0.2.0", "built from a tag"),
        ("v0.2", "is not"),
        ("v0.2.0-beta1", "is not"),
        ("v0.2.0-nightly.20261014", "is not"),
        (None, "built from a tag"),
    ],
)
def test_a_tag_must_name_the_version_under_development(tag: str | None, message: str) -> None:
    with pytest.raises(v.VersionError, match=message):
        v.compute("release", "0.2.0", tag=tag)


def test_the_nightly_date_is_checked() -> None:
    with pytest.raises(v.VersionError):
        v.compute("nightly", "0.2.0", date="2026-10-14")


def test_channels_and_python_forms() -> None:
    assert [v.channel(x) for x in ("1.2.3", "1.2.3-rc4", "1.2.3-nightly.20261014")] == [
        "stable",
        "candidate",
        "nightly",
    ]
    assert v.pep440("1.2.3-nightly.20261014") == "1.2.3.dev20261014"
    assert v.pep440("1.2.3-rc4") == "1.2.3rc4"
    with pytest.raises(v.VersionError):
        v.channel("1.2")
    with pytest.raises(v.VersionError):
        v.pep440("1.2.3-beta")


def test_stamp_writes_every_file_and_the_semver_stays_semver(tmp_path: Path) -> None:
    for name in (
        "VERSION",
        "controller/pyproject.toml",
        "controller/switchyard/__init__.py",
        "deploy/helm/switchyard/Chart.yaml",
    ):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / name, tmp_path / name)
    changed = v.stamp("0.1.0-nightly.20261014", tmp_path)
    assert changed == [
        "controller/pyproject.toml",
        "controller/switchyard/__init__.py",
        "deploy/helm/switchyard/Chart.yaml",
    ]
    assert 'version = "0.1.0.dev20261014"' in (tmp_path / "controller/pyproject.toml").read_text()
    assert (
        '__version__ = "0.1.0-nightly.20261014"'
        in (tmp_path / "controller/switchyard/__init__.py").read_text()
    )
    chart = (tmp_path / "deploy/helm/switchyard/Chart.yaml").read_text()
    assert (
        "version: 0.1.0-nightly.20261014" in chart
        and 'appVersion: "0.1.0-nightly.20261014"' in chart
    )
    assert v.check(tmp_path) != []  # a stamped copy no longer matches VERSION, which is expected
    assert v.stamp("0.1.0-nightly.20261014", tmp_path) == []  # stamping again changes nothing


def test_stamp_refuses_a_layout_it_does_not_know(tmp_path: Path) -> None:
    for name in ("controller/pyproject.toml", "controller/switchyard/__init__.py"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("nothing here\n")
    with pytest.raises(v.VersionError, match="no line matches"):
        v.stamp("0.2.0", tmp_path)


def test_the_stamped_controller_still_builds_a_valid_python_version() -> None:
    from packaging.version import Version

    for s in ("0.2.0", "0.2.0-rc1", "0.2.0-nightly.20261014"):
        Version(v.pep440(s))

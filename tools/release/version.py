#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Version numbers for nightly and release builds (ADR 0206). Standard library only.

The ``VERSION`` file at the repository root names the version under development, for example
``0.2.0``. A **nightly** is ``0.2.0-nightly.20261014``; a **release** is built from a tag ``v0.2.0`` (or
``v0.2.0-rc1``), and the tag must name the version in ``VERSION``. ``stamp`` writes a version into the
files that carry one (in the build's working copy, never committed). CI calls this; so can you.

    python3 tools/release/version.py compute nightly --date 20261014   -> 0.2.0-nightly.20261014
    python3 tools/release/version.py compute release --tag v0.2.0      -> 0.2.0
    python3 tools/release/version.py channel 0.2.0-rc1                 -> candidate
    python3 tools/release/version.py stamp 0.2.0-nightly.20261014
    python3 tools/release/version.py check                             -> exit 1 on a mismatch
"""

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_CORE = r"(\d+)\.(\d+)\.(\d+)"
_RELEASE = re.compile(rf"^{_CORE}$")
_RC = re.compile(rf"^{_CORE}-rc\.?(\d+)$")
_NIGHTLY = re.compile(rf"^{_CORE}-nightly\.(\d{{8}})$")


class VersionError(Exception):
    """A version or tag that the release process does not accept."""


def read_version(root: Path = ROOT) -> str:
    """The version under development, from the ``VERSION`` file.

    Raises:
        VersionError: The file is missing or does not hold ``MAJOR.MINOR.PATCH``.
    """
    try:
        text = (root / "VERSION").read_text().strip()
    except OSError as exc:
        raise VersionError("VERSION file not found") from exc
    if not _RELEASE.match(text):
        raise VersionError(f"VERSION must be MAJOR.MINOR.PATCH, not {text!r}")
    return text


def compute(mode: str, base: str, *, tag: str | None = None, date: str | None = None) -> str:
    """The version string for a build.

    ``nightly`` appends ``-nightly.<date>`` to ``base``. ``release`` takes the tag without its ``v``;
    the core numbers must equal ``base``. ``dev`` is ``base-dev``.

    Example:
        >>> compute("nightly", "0.2.0", date="20261014")
        '0.2.0-nightly.20261014'
        >>> compute("release", "0.2.0", tag="v0.2.0-rc1")
        '0.2.0-rc1'
    """
    if mode == "nightly":
        stamp = date or datetime.now(UTC).strftime("%Y%m%d")
        if not re.fullmatch(r"\d{8}", stamp):
            raise VersionError("the nightly date is YYYYMMDD")
        return f"{base}-nightly.{stamp}"
    if mode == "dev":
        return f"{base}-dev"
    if mode != "release":
        raise VersionError(f"unknown mode {mode!r}")
    if not tag or not tag.startswith("v"):
        raise VersionError("a release is built from a tag such as v0.2.0")
    version = tag[1:]
    m = _RELEASE.match(version) or _RC.match(version)
    if not m:
        raise VersionError(f"tag {tag!r} is not vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rcN")
    if ".".join(m.groups()[:3]) != base:
        raise VersionError(
            f"tag {tag} does not match VERSION ({base}): bump VERSION first, or tag {base}"
        )
    return version.replace("-rc.", "-rc")


def channel(version: str) -> str:
    """``stable``, ``candidate`` or ``nightly`` for a version string.

    Example:
        >>> [channel(v) for v in ("1.0.0", "1.0.0-rc2", "1.0.0-nightly.20261014")]
        ['stable', 'candidate', 'nightly']
    """
    if _RELEASE.match(version):
        return "stable"
    if _RC.match(version):
        return "candidate"
    if _NIGHTLY.match(version):
        return "nightly"
    raise VersionError(f"{version!r} is not a version the release process produces")


def pep440(version: str) -> str:
    """The same version as Python packaging writes it (``0.2.0.dev20261014``, ``0.2.0rc1``).

    Example:
        >>> [pep440(v) for v in ("0.2.0", "0.2.0-rc1", "0.2.0-nightly.20261014", "0.2.0-dev")]
        ['0.2.0', '0.2.0rc1', '0.2.0.dev20261014', '0.2.0.dev0']
    """
    if _RELEASE.match(version):
        return version
    if m := _RC.match(version):
        return f"{'.'.join(m.groups()[:3])}rc{m[4]}"
    if m := _NIGHTLY.match(version):
        return f"{'.'.join(m.groups()[:3])}.dev{m[4]}"
    if version.endswith("-dev") and _RELEASE.match(version[:-4]):
        return f"{version[:-4]}.dev0"
    raise VersionError(f"{version!r} is not a version the release process produces")


#: (file, pattern that holds the version on one line, replacement for that line given the version)
_TARGETS: list[tuple[str, str, str]] = [
    ("controller/pyproject.toml", r'^version = ".*"$', 'version = "{pep}"'),
    ("controller/switchyard/__init__.py", r'^__version__ = ".*"$', '__version__ = "{v}"'),
    ("deploy/helm/switchyard/Chart.yaml", r"^version: .*$", "version: {v}"),
    ("deploy/helm/switchyard/Chart.yaml", r"^appVersion: .*$", 'appVersion: "{v}"'),
]


def stamp(version: str, root: Path = ROOT) -> list[str]:
    """Write ``version`` into the files that carry one. Returns the files changed.

    Raises:
        VersionError: A file does not have the line to change (the layout moved).
    """
    channel(version)  # refuses anything else
    changed: list[str] = []
    for name, pattern, template in _TARGETS:
        path = root / name
        text = path.read_text()
        new, n = re.subn(
            pattern, template.format(v=version, pep=pep440(version)), text, count=1, flags=re.M
        )
        if n != 1:
            raise VersionError(f"{name}: no line matches {pattern}")
        if new != text:
            path.write_text(new)
            changed.append(name)
    return sorted(set(changed))


def check(root: Path = ROOT) -> list[str]:
    """Problems where the files that carry a version disagree with ``VERSION`` (empty: fine)."""
    base = read_version(root)
    problems = []
    for name, pattern, template in _TARGETS:
        text = (root / name).read_text()
        want = template.format(v=base, pep=pep440(base))
        line = re.search(pattern, text, flags=re.M)
        if line is None or line.group(0) != want:
            problems.append(f"{name}: expected {want!r}, found {line.group(0) if line else None!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print the version under development")
    c = sub.add_parser("compute", help="print the version for a build")
    c.add_argument("mode", choices=["nightly", "release", "dev"])
    c.add_argument("--tag")
    c.add_argument("--date")
    s = sub.add_parser("stamp", help="write a version into the build's files")
    s.add_argument("version")
    ch = sub.add_parser("channel", help="print stable, candidate or nightly")
    ch.add_argument("version")
    pe = sub.add_parser("pep440", help="print the Python packaging form")
    pe.add_argument("version")
    sub.add_parser("check", help="fail if the files disagree with VERSION")
    a = p.parse_args(argv)
    try:
        if a.cmd == "show":
            print(read_version())
        elif a.cmd == "compute":
            print(compute(a.mode, read_version(), tag=a.tag, date=a.date))
        elif a.cmd == "stamp":
            for name in stamp(a.version):
                print(f"stamped {name}")
        elif a.cmd == "channel":
            print(channel(a.version))
        elif a.cmd == "pep440":
            print(pep440(a.version))
        else:
            problems = check()
            for line in problems:
                print(line, file=sys.stderr)
            return 1 if problems else 0
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

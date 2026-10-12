# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Load golden cases: ``tests/golden/<case>/input.yaml`` -> files compared with ``expected/``.

``input.yaml`` is a snapshot mapping (symbolic ids, see :mod:`switchyard.domain.loader`) plus a
``render:`` block naming the SE group. Run ``UPDATE_GOLDEN=1 pytest tests/unit/render`` to rewrite
the expected files after an intentional renderer change, then review the diff.
"""

import os
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.render.haproxy import RenderResult, render_haproxy

GOLDEN_DIR = Path(__file__).parent / "golden"
WARNINGS_FILE = "warnings.txt"


def case_names() -> list[str]:
    """Every golden case directory, sorted."""
    return sorted(p.name for p in GOLDEN_DIR.iterdir() if (p / "input.yaml").is_file())


def render_case(name: str) -> RenderResult:
    """Render one golden case."""
    data: dict[str, Any] = yaml.safe_load((GOLDEN_DIR / name / "input.yaml").read_text("utf-8"))
    group = UUID(symbolic_uuid(data.pop("render")["se_group"]))
    return render_haproxy(snapshot_from_mapping(data), group)


def expected_files(name: str) -> dict[str, str]:
    """Read the expected output files of a case (relative path -> content)."""
    root = GOLDEN_DIR / name / "expected"
    return {
        p.relative_to(root).as_posix(): p.read_bytes().decode("utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def update_requested() -> bool:
    """True when ``UPDATE_GOLDEN=1`` asks to rewrite expected files."""
    return os.environ.get("UPDATE_GOLDEN") == "1"


def write_expected(name: str, result: RenderResult) -> None:
    """Replace a case's expected directory with ``result`` (LF line endings)."""
    root = GOLDEN_DIR / name / "expected"
    for old in sorted(root.rglob("*"), reverse=True):
        old.unlink() if old.is_file() else old.rmdir()
    files = dict(result.files)
    if result.warnings:
        files[WARNINGS_FILE] = "".join(f"{w}\n" for w in result.warnings)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))


def rendered_with_warnings(result: RenderResult) -> dict[str, str]:
    """Files plus the warnings file, in the shape ``expected_files`` returns."""
    files = dict(result.files)
    if result.warnings:
        files[WARNINGS_FILE] = "".join(f"{w}\n" for w in result.warnings)
    return files

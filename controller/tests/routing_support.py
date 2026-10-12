# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Golden cases for the routing renderers: ``tests/golden_routing/<case>/input.yaml``.

``render:`` names the SE group, the engines and the (fake) secrets; expected files live under
``expected/<engine>/``. ``UPDATE_GOLDEN=1`` rewrites them.
"""

import os
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.render.routing import EngineSlot, render_agent_bundle

GOLDEN_DIR = Path(__file__).parent / "golden_routing"


def case_names() -> list[str]:
    """Every case directory, sorted."""
    return sorted(p.name for p in GOLDEN_DIR.iterdir() if (p / "input.yaml").is_file())


def render_case(name: str) -> dict[str, dict[str, str]]:
    """Render a case: ``engine name -> files``."""
    data: dict[str, Any] = yaml.safe_load((GOLDEN_DIR / name / "input.yaml").read_text("utf-8"))
    spec = data.pop("render")
    snapshot = snapshot_from_mapping(data)
    group = UUID(symbolic_uuid(spec["se_group"]))
    out = {}
    for engine in spec["engines"]:
        slot = EngineSlot(engine["name"], engine["index"], engine["router_id"])
        out[slot.name] = render_agent_bundle(snapshot, group, slot, spec.get("secrets", {}))
    return out


def expected(name: str) -> dict[str, dict[str, str]]:
    """Read the expected files of a case."""
    root = GOLDEN_DIR / name / "expected"
    out: dict[str, dict[str, str]] = {}
    for engine in sorted(p for p in root.iterdir() if p.is_dir()):
        out[engine.name] = {
            p.relative_to(engine).as_posix(): p.read_bytes().decode("utf-8")
            for p in sorted(engine.rglob("*"))
            if p.is_file()
        }
    return out


def write_expected(name: str, rendered: dict[str, dict[str, str]]) -> None:
    """Replace a case's expected directory."""
    root = GOLDEN_DIR / name / "expected"
    for old in sorted(root.rglob("*"), reverse=True) if root.exists() else []:
        old.unlink() if old.is_file() else old.rmdir()
    for engine, files in rendered.items():
        for rel, content in files.items():
            path = root / engine / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))


def update_requested() -> bool:
    """True when ``UPDATE_GOLDEN=1``."""
    return os.environ.get("UPDATE_GOLDEN") == "1"

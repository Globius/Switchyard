# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rendered fragments per object: golden files, and agreement with the change-set diff."""

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from switchyard.domain import models as dm
from switchyard.domain.changes import COLLECTION, ObjectType
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import Snapshot
from switchyard.render.fragments import RENDERED_TYPES, Fragment, rendered_fragments
from switchyard.render.haproxy import render_haproxy
from switchyard.services.changesets import render_diffs
from tests.golden_support import GOLDEN_DIR, update_requested

CASES = ("03-tls-to-backend", "04-path-redirects", "11-inventory-mirror")
OUT = Path(__file__).parents[2] / "golden_fragments"
ORDER = sorted(RENDERED_TYPES, key=lambda t: t.value)


def snapshot_of(case: str) -> Snapshot:
    data: dict[str, Any] = yaml.safe_load((GOLDEN_DIR / case / "input.yaml").read_text("utf-8"))
    data.pop("render")
    return snapshot_from_mapping(data)


def selected(f: Fragment) -> str:
    if f.range is None:
        return f.content
    lines = f.content.splitlines(keepends=True)
    return "".join(lines[f.range[0] - 1 : f.range[1]])


def describe(fragments: list[Fragment]) -> str:
    out = []
    for f in fragments:
        where = "whole file" if f.range is None else f"lines {f.range[0]}-{f.range[1]}"
        out.append(f"==> {f.file} ({f.language}, {where})\n{selected(f)}")
    return "\n".join(out) if out else "(renders nothing)\n"


def objects(snapshot: Snapshot) -> list[tuple[ObjectType, Any]]:
    return [
        (t, o)
        for t in ORDER
        for o in sorted(getattr(snapshot, COLLECTION[t]), key=lambda o: o.name)
    ]


@pytest.mark.parametrize("case", CASES)
def test_fragments_match_golden_files(case: str) -> None:
    snapshot = snapshot_of(case)
    seen = set()
    for object_type, obj in objects(snapshot):
        seen.add(object_type)
        path = OUT / case / f"{object_type.value}__{obj.name}.txt"
        text = describe(rendered_fragments(snapshot, object_type, obj.id))
        if update_requested():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
        assert path.read_bytes().decode("utf-8") == text, path.name
    assert seen, "case has no renderable objects"


def test_every_renderable_type_is_covered_by_a_golden_case() -> None:
    covered = {t for case in CASES for t, _ in objects(snapshot_of(case))}
    assert covered == set(RENDERED_TYPES)


@pytest.mark.parametrize("case", CASES)
def test_fragments_are_slices_of_the_rendered_files(case: str) -> None:
    snapshot = snapshot_of(case)
    for object_type, obj in objects(snapshot):
        for f in rendered_fragments(snapshot, object_type, obj.id):
            result = render_haproxy(snapshot, f.se_group_id)
            assert f.content == result.files[f.file]
            assert f.fingerprint == result.fingerprint
            if f.range is not None:
                lo, hi = f.range
                assert 1 <= lo <= hi <= len(f.content.splitlines())


@pytest.mark.parametrize("case", CASES)
def test_rendering_twice_gives_identical_fragments(case: str) -> None:
    snapshot = snapshot_of(case)
    for object_type, obj in objects(snapshot):
        assert rendered_fragments(snapshot, object_type, obj.id) == rendered_fragments(
            snapshot, object_type, obj.id
        )


def test_types_without_haproxy_text_are_rejected() -> None:
    with pytest.raises(ValueError, match="no rendered HAProxy text"):
        rendered_fragments(snapshot_of(CASES[0]), ObjectType.NETWORK, UUID(int=1))


def test_draft_and_live_differ_exactly_where_the_diff_says() -> None:
    live = snapshot_of("03-tls-to-backend")
    pool = live.pools[0]
    draft = live.model_copy(
        update={
            "pools": (pool.model_copy(update={"algorithm": dm.Algorithm.SOURCE}), *live.pools[1:]),
        }
    )
    changed = {f.path for g in render_diffs(live, draft) for f in g.files if f.status == "modified"}
    before = rendered_fragments(live, ObjectType.POOL, pool.id)
    after = rendered_fragments(draft, ObjectType.POOL, pool.id)
    assert {f.file for f in after} <= changed
    assert [selected(f) for f in before] != [selected(f) for f in after]
    assert before[0].fingerprint != after[0].fingerprint
    # Objects the change did not touch keep their text.
    other = next(p for p in live.pools if p.id != pool.id)
    assert [selected(f) for f in rendered_fragments(live, ObjectType.POOL, other.id)] == [
        selected(f) for f in rendered_fragments(draft, ObjectType.POOL, other.id)
    ]

"""The permission matrix in code, in the design document and in the API must say the same."""

from pathlib import Path
from typing import get_args

from switchyard.api.schemas import PermissionObject, PermissionVerb
from switchyard.domain import permissions
from switchyard.domain.workflow import Role

DOC = Path(__file__).parents[4] / "docs" / "design" / "02-backend-design.md"
CHECK = "✅"


def doc_rows() -> list[tuple[str, str, list[str]]]:
    """The rows of the matrix table in 02: (verb, object, the four role cells)."""
    lines = DOC.read_text("utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Verb | Object |"))
    rows: list[tuple[str, str, list[str]]] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append((cells[0], cells[1], cells[2:6]))
    return rows


def test_every_entry_matches_its_row_in_the_design_document() -> None:
    rows = doc_rows()
    by_row: dict[int, list[permissions.Permission]] = {}
    for entry in permissions.MATRIX:
        by_row.setdefault(entry.row, []).append(entry)
    assert sorted(by_row) == list(range(1, len(rows) + 1)), "every table row needs an entry"
    for number, (verb, obj, cells) in enumerate(rows, start=1):
        # roles nest: the check marks must start at one role and continue to the strongest
        marks = [CHECK in cell for cell in cells]
        first = marks.index(True) if True in marks else None
        if first is not None:
            assert all(marks[first:]), f"row {number} ({verb} {obj}): marks must nest"
        weakest = permissions.ROLES[first] if first is not None else None
        own_only = "own action" in cells[1]
        for entry in by_row[number]:
            if entry.object.endswith("_own"):
                assert own_only, f"row {number}: only the 'own action' row has an _own entry"
                assert entry.min_role is Role.OPERATOR
            elif own_only:
                assert entry.min_role is weakest, f"row {number} ({verb} {obj})"
            else:
                assert entry.min_role is weakest, (
                    f"row {number} ({verb} {obj}): code says {entry.min_role}, doc says {weakest}"
                )


def test_the_published_verbs_and_objects_are_exactly_the_matrix() -> None:
    assert set(get_args(PermissionVerb)) == {p.verb for p in permissions.MATRIX}
    assert set(get_args(PermissionObject)) == {p.object for p in permissions.MATRIX}


def test_every_entry_has_a_description_and_a_unique_name() -> None:
    names = [(p.verb, p.object) for p in permissions.MATRIX]
    assert len(set(names)) == len(names)
    assert all(p.description.endswith(".") for p in permissions.MATRIX)


def test_roles_nest() -> None:
    assert permissions.allows(Role.SYSTEM_ADMIN, "manage", "engine")
    assert permissions.allows(Role.TENANT_ADMIN, "apply", "change_set")
    assert not permissions.allows(Role.OPERATOR, "approve", "change_set")
    assert not permissions.allows(Role.TENANT_ADMIN, "apply_bypass", "change_set")


def test_every_entry_has_one_of_the_eight_groups() -> None:
    from switchyard.domain.permissions import GROUP_OF_ROW, MATRIX

    assert {p.group for p in MATRIX} == {
        "Read",
        "Applications",
        "Infrastructure",
        "Change sets",
        "Runtime",
        "Tenant settings",
        "Access",
        "System",
    }
    assert {p.row for p in MATRIX} <= set(GROUP_OF_ROW), "a new row needs a group"
    by_verb = {(p.verb, p.object): p.group for p in MATRIX}
    assert by_verb[("manage", "engine")] == "Infrastructure"
    assert by_verb[("rollback", "revision")] == "Change sets"
    assert by_verb[("set_state", "member")] == "Runtime"

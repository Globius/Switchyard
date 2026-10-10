"""Every audit action the code writes is in the published catalog."""

import ast
from pathlib import Path

from switchyard.db.audit_actions import CATALOG

ROOT = Path(__file__).parents[2] / "switchyard"


def _literal_actions() -> set[str]:
    found: set[str] = set()
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "action":
                        values = [kw.value]
                        if isinstance(kw.value, ast.IfExp):
                            values = [kw.value.body, kw.value.orelse]
                        found |= {
                            v.value
                            for v in values
                            if isinstance(v, ast.Constant) and isinstance(v.value, str)
                        }
    # Actions passed to a helper as a plain argument (``_audit(..., "job.retried", ...)``).
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith("job.")
                and node.value.count(".") == 1
            ):
                found.add(node.value)
    return found


def test_the_catalog_lists_every_action_in_the_code() -> None:
    known = {a.action for a in CATALOG}
    missing = sorted(_literal_actions() - known)
    assert not missing, f"add these to switchyard/db/audit_actions.py: {missing}"
    assert len(known) == len(CATALOG)
    assert {"engine.enabled", "engine.disabled"} <= known  # built from `engine.{state}`

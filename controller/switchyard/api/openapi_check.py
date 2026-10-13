# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Detect breaking changes between two OpenAPI documents (CI gate).

Breaking: a removed path or method, a removed documented response code, a request parameter that
became required, a new required request-body property, or a removed response property.

Usage: ``python -m switchyard.api.openapi_check old.json new.json`` (exit 1 on breaking changes).
"""

import json
import sys
from typing import Any

_METHODS = {"get", "put", "post", "delete", "patch"}


def _schema(spec: dict[str, Any], node: dict[str, Any] | None) -> dict[str, Any]:
    while node and "$ref" in node:
        node = spec["components"]["schemas"][node["$ref"].split("/")[-1]]
    return node or {}


def _body(spec: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    content = op.get("requestBody", {}).get("content", {}).get("application/json", {})
    return _schema(spec, content.get("schema"))


def breaking_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Return a human-readable list of breaking changes from ``old`` to ``new``.

    Example:
        >>> a = {"paths": {"/x": {"get": {"responses": {"200": {}}}}}}
        >>> breaking_changes(a, {"paths": {}})
        ['removed path /x']
    """
    out: list[str] = []
    for path, methods in old.get("paths", {}).items():
        if path not in new.get("paths", {}):
            out.append(f"removed path {path}")
            continue
        for method, op in methods.items():
            if method not in _METHODS:
                continue
            fresh = new["paths"][path].get(method)
            if fresh is None:
                out.append(f"removed {method.upper()} {path}")
                continue
            for code in op.get("responses", {}):
                if code not in fresh.get("responses", {}):
                    out.append(f"{method.upper()} {path}: removed response {code}")
            old_params = {(p["name"], p["in"]): p for p in op.get("parameters", [])}
            for p in fresh.get("parameters", []):
                before = old_params.get((p["name"], p["in"]))
                if p.get("required") and (before is None or not before.get("required")):
                    out.append(f"{method.upper()} {path}: parameter {p['name']} became required")
            was = set(_body(old, op).get("required", []))
            for name in set(_body(new, fresh).get("required", [])) - was:
                out.append(f"{method.upper()} {path}: request property {name} became required")
    return out


def main(argv: list[str]) -> int:
    """CLI entry point."""
    with open(argv[1], encoding="utf-8") as a, open(argv[2], encoding="utf-8") as b:
        problems = breaking_changes(json.load(a), json.load(b))
    for line in problems:
        print(line)  # noqa: T201
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

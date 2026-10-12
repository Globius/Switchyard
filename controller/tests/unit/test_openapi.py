# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The committed OpenAPI document is the API contract; changes must be deliberate."""

import json
import os
from pathlib import Path

from switchyard.api.app import create_app
from switchyard.api.openapi_check import breaking_changes

SNAPSHOT = Path(__file__).parents[3] / "docs" / "api" / "openapi.json"


def current() -> dict:  # type: ignore[type-arg]
    return create_app(None).openapi()  # type: ignore[arg-type]


def test_snapshot_matches_the_application() -> None:
    text = json.dumps(current(), indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_OPENAPI") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_bytes(text.encode())
    assert SNAPSHOT.read_text("utf-8") == text, "run UPDATE_OPENAPI=1 pytest and review the diff"


def test_breaking_change_detector() -> None:
    old = {
        "paths": {
            "/a": {
                "get": {
                    "parameters": [{"name": "q", "in": "query"}],
                    "responses": {"200": {}, "404": {}},
                },
                "delete": {"responses": {"204": {}}},
            }
        }
    }
    new = {
        "paths": {
            "/a": {
                "get": {
                    "parameters": [{"name": "q", "in": "query", "required": True}],
                    "responses": {"200": {}},
                }
            }
        }
    }
    assert breaking_changes(old, new) == [
        "GET /a: removed response 404",
        "GET /a: parameter q became required",
        "removed DELETE /a",
    ] or sorted(breaking_changes(old, new)) == sorted(
        ["GET /a: removed response 404", "GET /a: parameter q became required", "removed DELETE /a"]
    )
    assert breaking_changes(old, old) == []

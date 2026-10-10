"""Build a :class:`Snapshot` from a plain mapping (parsed YAML or JSON).

Hand-written fixtures and the demo data would be unreadable with raw UUIDs everywhere, so this
loader lets authors use short symbolic ids. Any string under a key named ``id``, ``*_id`` or
``*_ids`` that is not already a UUID becomes a deterministic UUIDv5 of that string. The same input
always yields the same ids, so renders of fixtures are reproducible.

The loader is pure: callers do the file reading.

Example:
    >>> snap = snapshot_from_mapping({"tenants": [{"id": "acme", "name": "acme"}]})
    >>> str(snap.tenants[0].id) == symbolic_uuid("acme")
    True
"""

import uuid
from collections.abc import Mapping
from typing import Any

from switchyard.domain.snapshot import Snapshot

#: Fixed namespace so symbolic ids map to the same UUIDs everywhere.
FIXTURE_NAMESPACE = uuid.UUID("5a5e7d6c-0000-4000-8000-000000005957")


def symbolic_uuid(value: str) -> str:
    """Return ``value`` if it is a UUID, else a deterministic UUIDv5 derived from it.

    Example:
        >>> symbolic_uuid("00000000-0000-0000-0000-000000000001")
        '00000000-0000-0000-0000-000000000001'
        >>> symbolic_uuid("web") == symbolic_uuid("web")
        True
    """
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return str(uuid.uuid5(FIXTURE_NAMESPACE, value))


def _is_id_key(key: str) -> bool:
    return key == "id" or key.endswith("_id") or key.endswith("_ids")


def _convert(node: Any, key: str | None = None) -> Any:
    if isinstance(node, Mapping):
        return {k: _convert(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_convert(v, key) for v in node]
    if isinstance(node, str) and key is not None and _is_id_key(key):
        return symbolic_uuid(node)
    return node


def snapshot_from_mapping(data: Mapping[str, Any]) -> Snapshot:
    """Validate ``data`` into a :class:`Snapshot`, expanding symbolic ids.

    Pool members need ids too; a member without an ``id`` gets one derived from its pool id and
    position, so authors can omit them.

    Raises:
        pydantic.ValidationError: If any object is invalid.
    """
    converted = _convert(data)
    for pool in converted.get("pools", []):
        for index, member in enumerate(pool.get("members", [])):
            member.setdefault("id", symbolic_uuid(f"{pool.get('id', '')}/member/{index}"))
    return Snapshot.model_validate(converted)

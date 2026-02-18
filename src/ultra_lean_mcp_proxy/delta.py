"""Delta response helpers for Ultra Lean MCP Proxy v2.

Produces structural JSON diff ops (set/delete), matching the Node.js
implementation for cross-runtime format parity.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional


def canonicalize(value: Any) -> Any:
    """Canonicalize JSON-like data for stable hashing/diffing."""
    if isinstance(value, dict):
        return {k: canonicalize(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [canonicalize(v) for v in value]
    return value


def _clone_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def stable_hash(value: Any) -> str:
    text = json.dumps(canonicalize(value), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _deep_equal(a: Any, b: Any) -> bool:
    return json.dumps(canonicalize(a), separators=(",", ":")) == json.dumps(
        canonicalize(b), separators=(",", ":")
    )


def _diff_values(previous: Any, current: Any, path: list, ops: list) -> None:
    """Recursively compute structural diff ops between two JSON values."""
    if _deep_equal(previous, current):
        return

    if isinstance(previous, list) and isinstance(current, list):
        if len(previous) != len(current):
            ops.append({"op": "set", "path": list(path), "value": _clone_json(current)})
            return
        for i in range(len(current)):
            _diff_values(previous[i], current[i], path + [i], ops)
        return

    if isinstance(previous, dict) and isinstance(current, dict):
        keys = sorted(set(list(previous.keys()) + list(current.keys())))
        for key in keys:
            if key not in current:
                ops.append({"op": "delete", "path": path + [key]})
                continue
            if key not in previous:
                ops.append({"op": "set", "path": path + [key], "value": _clone_json(current[key])})
                continue
            _diff_values(previous[key], current[key], path + [key], ops)
        return

    ops.append({"op": "set", "path": list(path), "value": _clone_json(current)})


def create_delta(
    previous: Any,
    current: Any,
    min_savings_ratio: float = 0.15,
    max_patch_bytes: int = 65536,
) -> Optional[dict[str, Any]]:
    """Create a structural JSON diff envelope if it saves enough bytes."""
    canonical_previous = canonicalize(previous)
    canonical_current = canonicalize(current)
    if _deep_equal(canonical_previous, canonical_current):
        return None

    ops: list[dict[str, Any]] = []
    _diff_values(canonical_previous, canonical_current, [], ops)
    if not ops:
        return None

    patch_bytes = _json_bytes(ops)
    full_bytes = _json_bytes(canonical_current)
    if patch_bytes > max_patch_bytes:
        return None

    savings_ratio = (full_bytes - patch_bytes) / full_bytes if full_bytes > 0 else 0.0
    if savings_ratio < min_savings_ratio:
        return None

    return {
        "encoding": "lapc-delta-v1",
        "baselineHash": stable_hash(canonical_previous),
        "currentHash": stable_hash(canonical_current),
        "ops": ops,
        "patchBytes": patch_bytes,
        "fullBytes": full_bytes,
        "savedBytes": full_bytes - patch_bytes,
        "savedRatio": savings_ratio,
    }


def _get_parent_for_path(root: Any, path: list) -> tuple[Any, Any]:
    """Navigate to the parent container for the last segment of path."""
    if not path:
        return None, None
    cursor = root
    for i in range(len(path) - 1):
        segment = path[i]
        next_segment = path[i + 1]
        if isinstance(cursor, list):
            idx = int(segment)
            if idx < 0:
                raise ValueError("Invalid array index in delta path")
            while len(cursor) <= idx:
                cursor.append(None)
            if cursor[idx] is None:
                cursor[idx] = [] if isinstance(next_segment, int) else {}
            cursor = cursor[idx]
            continue
        if not isinstance(cursor, dict):
            raise ValueError("Invalid delta path parent")
        if segment not in cursor or cursor[segment] is None:
            cursor[segment] = [] if isinstance(next_segment, int) else {}
        cursor = cursor[segment]
    return cursor, path[-1]


def apply_delta(previous: Any, delta: dict[str, Any]) -> Any:
    """Apply a structural JSON diff to a previous payload."""
    if not isinstance(delta, dict) or delta.get("encoding") != "lapc-delta-v1":
        raise ValueError("Unsupported delta envelope")
    ops = delta.get("ops")
    if not isinstance(ops, list):
        raise ValueError("Delta envelope missing ops")

    output = _clone_json(previous)
    for op in ops:
        if not isinstance(op, dict) or not isinstance(op.get("path"), list):
            raise ValueError("Invalid delta op")
        path = op["path"]
        if op.get("op") == "set":
            if not path:
                output = _clone_json(op.get("value"))
                continue
            parent, key = _get_parent_for_path(output, path)
            if isinstance(parent, list):
                idx = int(key)
                if idx < 0:
                    raise ValueError("Invalid array index in set op")
                while len(parent) <= idx:
                    parent.append(None)
                parent[idx] = _clone_json(op.get("value"))
            elif isinstance(parent, dict):
                parent[key] = _clone_json(op.get("value"))
            else:
                raise ValueError("Invalid set op parent")
            continue

        if op.get("op") == "delete":
            if not path:
                output = None
                continue
            parent, key = _get_parent_for_path(output, path)
            if isinstance(parent, list):
                idx = int(key)
                if 0 <= idx < len(parent):
                    parent.pop(idx)
            elif isinstance(parent, dict):
                parent.pop(key, None)
            continue

        raise ValueError(f"Unsupported delta op: {op.get('op')}")
    return output

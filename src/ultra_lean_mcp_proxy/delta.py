"""Delta response helpers for Ultra Lean MCP Proxy v2."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from typing import Any


def canonicalize(value: Any) -> Any:
    """Canonicalize JSON-like data for stable hashing/diffing."""
    if isinstance(value, dict):
        return {k: canonicalize(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [canonicalize(v) for v in value]
    return value


def stable_hash(value: Any) -> str:
    text = json.dumps(canonicalize(value), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_text(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"


def create_delta(
    previous: Any,
    current: Any,
    min_savings_ratio: float = 0.15,
    max_patch_bytes: int = 65536,
) -> dict[str, Any] | None:
    """Create a unified diff envelope if it is smaller than full payload."""
    prev_text = _canonical_text(previous)
    curr_text = _canonical_text(current)
    if prev_text == curr_text:
        return None

    diff_lines = difflib.unified_diff(
        prev_text.splitlines(keepends=True),
        curr_text.splitlines(keepends=True),
        fromfile="previous",
        tofile="current",
    )
    patch = "".join(diff_lines)
    if not patch:
        return None
    patch_bytes = len(patch.encode("utf-8"))
    full_bytes = len(curr_text.encode("utf-8"))
    if patch_bytes > max_patch_bytes:
        return None

    savings_ratio = (full_bytes - patch_bytes) / full_bytes if full_bytes else 0.0
    if savings_ratio < min_savings_ratio:
        return None

    return {
        "encoding": "lapc-delta-v1",
        "baselineHash": stable_hash(previous),
        "currentHash": stable_hash(current),
        "patch": patch,
        "patchBytes": patch_bytes,
        "fullBytes": full_bytes,
        "savedBytes": full_bytes - patch_bytes,
        "savedRatio": savings_ratio,
    }


def apply_delta(previous: Any, delta: dict[str, Any]) -> Any:
    """Apply a unified diff patch to previous JSON payload."""
    if not isinstance(delta, dict) or delta.get("encoding") != "lapc-delta-v1":
        raise ValueError("Unsupported delta envelope")
    patch = delta.get("patch")
    if not isinstance(patch, str):
        raise ValueError("Delta envelope missing patch text")
    patched_text = _apply_unified_patch(_canonical_text(previous), patch)
    return json.loads(patched_text)


def _apply_unified_patch(original_text: str, patch_text: str) -> str:
    """Apply a unified diff patch to text."""
    original_lines = original_text.splitlines(keepends=True)
    patch_lines = patch_text.splitlines(keepends=True)

    out: list[str] = []
    src_idx = 0
    i = 0

    # Skip file headers
    while i < len(patch_lines) and (patch_lines[i].startswith("---") or patch_lines[i].startswith("+++")):
        i += 1

    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    while i < len(patch_lines):
        header = patch_lines[i]
        match = hunk_re.match(header)
        if not match:
            i += 1
            continue
        old_start = int(match.group(1)) - 1
        out.extend(original_lines[src_idx:old_start])
        src_idx = old_start
        i += 1

        while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
            line = patch_lines[i]
            if line.startswith(" "):
                out.append(original_lines[src_idx])
                src_idx += 1
            elif line.startswith("-"):
                src_idx += 1
            elif line.startswith("+"):
                out.append(line[1:])
            elif line.startswith("\\"):
                # "\ No newline at end of file"
                pass
            i += 1

    out.extend(original_lines[src_idx:])
    return "".join(out)



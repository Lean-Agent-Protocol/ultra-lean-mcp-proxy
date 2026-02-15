"""Helpers for the optional tools_hash_sync MCP extension."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from .delta import canonicalize

TOOLS_HASH_WIRE_RE = re.compile(r"^([a-z0-9_]+):([0-9a-f]{64})$")


def canonical_tools_json(tools_payload: Any) -> str:
    """Return canonical JSON text for a visible tools payload."""
    return json.dumps(canonicalize(tools_payload), separators=(",", ":"), ensure_ascii=False)


def compute_tools_hash(
    tools_payload: Any,
    *,
    algorithm: str = "sha256",
    include_server_fingerprint: bool = False,
    server_fingerprint: Optional[str] = None,
) -> str:
    """Compute wire-format hash for a visible tools payload."""
    if algorithm != "sha256":
        raise ValueError(f"Unsupported tools hash algorithm: {algorithm}")

    payload = canonicalize(tools_payload)
    preimage: Any = payload
    if include_server_fingerprint:
        preimage = {
            "tools": payload,
            "server_fingerprint": server_fingerprint or "",
        }
    text = json.dumps(preimage, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def parse_if_none_match(value: Any, *, expected_algorithm: str = "sha256") -> Optional[str]:
    """Validate and normalize `if_none_match` wire values.

    Returns normalized lowercase wire hash (`sha256:<hex>`) when valid, else None.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    match = TOOLS_HASH_WIRE_RE.match(candidate)
    if not match:
        return None
    if match.group(1) != expected_algorithm:
        return None
    return candidate


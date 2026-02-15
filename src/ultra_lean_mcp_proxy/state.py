"""Session/cache/tool-index state for Ultra Lean MCP Proxy."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Optional


def clone_json(value: Any) -> Any:
    """Clone JSON-serializable data via round-trip."""
    return json.loads(json.dumps(value))


def stable_json_dumps(value: Any) -> str:
    """Deterministic JSON serialization used for hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_hash(value: Any) -> str:
    text = stable_json_dumps(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def args_hash(arguments: Any) -> str:
    if arguments is None:
        return stable_hash({})
    return stable_hash(arguments)


def is_mutating_tool_name(tool_name: str) -> bool:
    verbs = [
        "create",
        "update",
        "delete",
        "remove",
        "set",
        "write",
        "insert",
        "patch",
        "post",
        "put",
        "merge",
        "upload",
        "commit",
        # Stateful browser/session-like operations that can invalidate read cache.
        "navigate",
        "open",
        "close",
        "click",
        "type",
        "press",
        "select",
        "hover",
        "drag",
        "drop",
        "scroll",
        "evaluate",
        "execute",
        "goto",
        "reload",
        "back",
        "forward",
    ]
    name = tool_name.lower()
    return any(v in name for v in verbs)


def make_cache_key(session_id: str, server_name: str, tool_name: str, arguments: Any) -> str:
    return f"{session_id}:{server_name}:{tool_name}:{args_hash(arguments)}"


@dataclass
class CacheEntry:
    value: Any
    expires_at: float
    created_at: float
    hits: int = 0


@dataclass
class ToolsHashEntry:
    last_hash: Optional[str] = None
    conditional_hits: int = 0
    updated_at: float = 0.0


class ProxyState:
    """In-memory state for one Ultra Lean MCP Proxy process."""

    def __init__(self, max_cache_entries: int = 5000):
        self.max_cache_entries = max(1, max_cache_entries)
        self._cache: dict[str, CacheEntry] = {}
        self._history: dict[str, Any] = {}
        self._tools: list[dict[str, Any]] = []
        self._tools_hash: dict[str, ToolsHashEntry] = {}

    # Cache
    def cache_get(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if not entry:
            return None
        now = time.time()
        if entry.expires_at < now:
            del self._cache[key]
            return None
        entry.hits += 1
        return clone_json(entry.value)

    def cache_set(self, key: str, value: Any, ttl_seconds: int):
        now = time.time()
        self._cache[key] = CacheEntry(
            value=clone_json(value),
            created_at=now,
            expires_at=now + max(0, ttl_seconds),
            hits=0,
        )
        self._evict_cache_if_needed()

    def cache_invalidate_prefix(self, prefix: str) -> int:
        removed = 0
        for key in list(self._cache.keys()):
            if key.startswith(prefix):
                self._cache.pop(key, None)
                removed += 1
        return removed

    def _evict_cache_if_needed(self):
        if len(self._cache) <= self.max_cache_entries:
            return
        # Evict lowest-hit then oldest.
        ordered = sorted(
            self._cache.items(),
            key=lambda item: (item[1].hits, item[1].created_at),
        )
        overflow = len(self._cache) - self.max_cache_entries
        for key, _ in ordered[:overflow]:
            self._cache.pop(key, None)

    # Delta history
    def history_get(self, key: str) -> Optional[Any]:
        value = self._history.get(key)
        if value is None:
            return None
        return clone_json(value)

    def history_set(self, key: str, value: Any):
        self._history[key] = clone_json(value)
        if len(self._history) > self.max_cache_entries * 2:
            # Soft bound: trim oldest inserted key.
            first_key = next(iter(self._history))
            self._history.pop(first_key, None)

    def history_invalidate_prefix(self, prefix: str) -> int:
        removed = 0
        for key in list(self._history.keys()):
            if key.startswith(prefix):
                self._history.pop(key, None)
                removed += 1
        return removed

    # Tools index
    def set_tools(self, tools: list[dict[str, Any]]):
        self._tools = clone_json(tools or [])

    def get_tools(self) -> list[dict[str, Any]]:
        return clone_json(self._tools)

    def search_tools(
        self,
        query: str,
        top_k: int = 8,
        include_schemas: bool = True,
    ) -> list[dict[str, Any]]:
        if not self._tools:
            return []

        terms = [t for t in re.findall(r"[a-zA-Z0-9_]+", query.lower()) if t]
        ranked = []
        for tool in self._tools:
            name = str(tool.get("name", ""))
            desc = str(tool.get("description", ""))
            schema = tool.get("inputSchema") or tool.get("input_schema") or {}
            props = schema.get("properties", {}) if isinstance(schema, dict) else {}
            param_text = " ".join(str(k) for k in props.keys())
            haystack = f"{name} {desc} {param_text}".lower()

            score = 0.0
            if query.lower() in name.lower():
                score += 4.0
            for term in terms:
                if term in name.lower():
                    score += 2.0
                if term in desc.lower():
                    score += 1.0
                if term in param_text.lower():
                    score += 1.25
                if term in haystack:
                    score += 0.2
            if score <= 0:
                continue
            ranked.append((score, tool))

        if not ranked:
            ranked = [(0.01, tool) for tool in self._tools]

        ranked.sort(key=lambda item: item[0], reverse=True)
        results = []
        for score, tool in ranked[: max(1, top_k)]:
            item = {
                "name": tool.get("name"),
                "score": round(score, 3),
                "description": tool.get("description", ""),
            }
            if include_schemas:
                schema = tool.get("inputSchema") or tool.get("input_schema")
                if schema is not None:
                    item["inputSchema"] = clone_json(schema)
            results.append(item)
        return results

    # tools_hash_sync scope state
    def tools_hash_get(self, key: str) -> Optional[ToolsHashEntry]:
        entry = self._tools_hash.get(key)
        if entry is None:
            return None
        return ToolsHashEntry(
            last_hash=entry.last_hash,
            conditional_hits=entry.conditional_hits,
            updated_at=entry.updated_at,
        )

    def tools_hash_set_last(self, key: str, tools_hash: str):
        now = time.time()
        entry = self._tools_hash.setdefault(key, ToolsHashEntry())
        if entry.last_hash != tools_hash:
            entry.conditional_hits = 0
        entry.last_hash = tools_hash
        entry.updated_at = now

    def tools_hash_record_hit(self, key: str) -> int:
        now = time.time()
        entry = self._tools_hash.setdefault(key, ToolsHashEntry())
        entry.conditional_hits += 1
        entry.updated_at = now
        return entry.conditional_hits

    def tools_hash_reset_hits(self, key: str):
        now = time.time()
        entry = self._tools_hash.setdefault(key, ToolsHashEntry())
        entry.conditional_hits = 0
        entry.updated_at = now


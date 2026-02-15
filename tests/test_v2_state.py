"""Tests for v2 proxy state management."""

import time

from ultra_lean_mcp_proxy.state import ProxyState, is_mutating_tool_name, make_cache_key


def test_cache_set_get_and_expire():
    state = ProxyState(max_cache_entries=10)
    key = make_cache_key("s1", "srv", "list_items", {"page": 1})
    state.cache_set(key, {"ok": True}, ttl_seconds=0)
    time.sleep(0.01)
    assert state.cache_get(key) is None


def test_cache_retrieves_cloned_value():
    state = ProxyState(max_cache_entries=10)
    key = make_cache_key("s1", "srv", "list_items", {"page": 1})
    state.cache_set(key, {"nested": {"value": 1}}, ttl_seconds=60)
    cached = state.cache_get(key)
    cached["nested"]["value"] = 999
    cached2 = state.cache_get(key)
    assert cached2["nested"]["value"] == 1


def test_cache_invalidate_prefix_removes_expected_entries():
    state = ProxyState(max_cache_entries=10)
    k1 = make_cache_key("s1", "srv", "list_items", {"page": 1})
    k2 = make_cache_key("s1", "srv", "read_item", {"id": "a"})
    k3 = make_cache_key("s2", "srv", "list_items", {"page": 1})
    state.cache_set(k1, {"ok": 1}, ttl_seconds=60)
    state.cache_set(k2, {"ok": 2}, ttl_seconds=60)
    state.cache_set(k3, {"ok": 3}, ttl_seconds=60)

    removed = state.cache_invalidate_prefix("s1:srv:")
    assert removed == 2
    assert state.cache_get(k1) is None
    assert state.cache_get(k2) is None
    assert state.cache_get(k3) == {"ok": 3}


def test_history_invalidate_prefix_removes_expected_entries():
    state = ProxyState(max_cache_entries=10)
    state.history_set("cache_raw:s1:srv:key1", {"a": 1})
    state.history_set("cache_raw:s1:srv:key2", {"a": 2})
    state.history_set("cache_raw:s2:srv:key3", {"a": 3})

    removed = state.history_invalidate_prefix("cache_raw:s1:srv:")
    assert removed == 2
    assert state.history_get("cache_raw:s1:srv:key1") is None
    assert state.history_get("cache_raw:s1:srv:key2") is None
    assert state.history_get("cache_raw:s2:srv:key3") == {"a": 3}


def test_search_tools_returns_ranked_matches():
    state = ProxyState(max_cache_entries=10)
    state.set_tools(
        [
            {
                "name": "list_pull_requests",
                "description": "List pull requests for repo",
                "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}},
            },
            {
                "name": "create_issue",
                "description": "Create an issue in repository",
                "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
            },
        ]
    )
    matches = state.search_tools("pull requests", top_k=2, include_schemas=False)
    assert matches
    assert matches[0]["name"] == "list_pull_requests"
    assert "inputSchema" not in matches[0]


def test_tools_hash_state_tracks_last_hash_and_hits():
    state = ProxyState(max_cache_entries=10)
    key = "session:server:profile"

    assert state.tools_hash_get(key) is None

    state.tools_hash_set_last(key, "sha256:abc")
    entry = state.tools_hash_get(key)
    assert entry is not None
    assert entry.last_hash == "sha256:abc"
    assert entry.conditional_hits == 0

    assert state.tools_hash_record_hit(key) == 1
    assert state.tools_hash_record_hit(key) == 2
    entry = state.tools_hash_get(key)
    assert entry is not None
    assert entry.conditional_hits == 2

    state.tools_hash_set_last(key, "sha256:def")
    entry = state.tools_hash_get(key)
    assert entry is not None
    assert entry.last_hash == "sha256:def"
    assert entry.conditional_hits == 0


def test_is_mutating_tool_name_includes_stateful_browser_actions():
    assert is_mutating_tool_name("puppeteer_navigate")
    assert is_mutating_tool_name("puppeteer_evaluate")
    assert is_mutating_tool_name("create_issue")
    assert not is_mutating_tool_name("read_graph")


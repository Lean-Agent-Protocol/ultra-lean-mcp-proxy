"""Tests for lazy loading modes, including the catalog mode."""

from __future__ import annotations

from ultra_lean_mcp_proxy.config import ProxyConfig
from ultra_lean_mcp_proxy.proxy import (
    SEARCH_TOOL_NAME,
    ProxyMetrics,
    _build_search_tool_definition,
    _handle_tools_list_result,
)
from ultra_lean_mcp_proxy.result_compression import TokenCounter
from ultra_lean_mcp_proxy.state import ProxyState, clone_json


def _make_tools(n: int) -> list[dict]:
    """Generate n sample tools with realistic schemas."""
    return [
        {
            "name": f"tool_{i}",
            "description": f"Description for tool {i} that does something useful.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "param_a": {"type": "string", "description": "First parameter"},
                    "param_b": {"type": "integer", "description": "Second parameter"},
                },
                "required": ["param_a"],
            },
        }
        for i in range(n)
    ]


def _cfg(*, lazy_mode: str = "catalog", min_tools: int = 5) -> ProxyConfig:
    return ProxyConfig(
        definition_compression_enabled=False,
        lazy_loading_enabled=True,
        lazy_mode=lazy_mode,
        lazy_min_tools=min_tools,
        lazy_min_tokens=500,
        tools_hash_sync_enabled=False,
    )


def _run(tools: list[dict], config: ProxyConfig) -> dict:
    result = {"tools": tools}
    state = ProxyState(max_cache_entries=100)
    metrics = ProxyMetrics()
    counter = TokenCounter()
    return _handle_tools_list_result(
        clone_json(result),
        state,
        config,
        metrics,
        counter,
        tools_hash_sync_negotiated=False,
        profile_fingerprint="test",
    )


# --- catalog mode tests ---


def test_catalog_mode_sends_bare_stubs_plus_search_tool():
    """Catalog mode should send bare-minimum callable entries + the search meta-tool."""
    tools = _make_tools(10)
    out = _run(tools, _cfg(lazy_mode="catalog"))
    # 10 bare stubs + 1 search tool
    assert len(out["tools"]) == 11
    names = [t["name"] for t in out["tools"]]
    assert SEARCH_TOOL_NAME in names
    for i in range(10):
        assert f"tool_{i}" in names


def test_catalog_mode_bare_stubs_have_no_description_or_properties():
    """Catalog bare stubs should have only name + empty object schema."""
    tools = _make_tools(10)
    out = _run(tools, _cfg(lazy_mode="catalog"))
    stubs = [t for t in out["tools"] if t["name"] != SEARCH_TOOL_NAME]
    for stub in stubs:
        assert "description" not in stub
        assert stub["inputSchema"] == {"type": "object"}


def test_catalog_mode_embeds_tool_names_in_description():
    """Catalog mode should list all tool names in the search tool description."""
    tools = _make_tools(10)
    out = _run(tools, _cfg(lazy_mode="catalog"))
    search_tool = next(t for t in out["tools"] if t["name"] == SEARCH_TOOL_NAME)
    desc = search_tool["description"]
    for i in range(10):
        assert f"tool_{i}" in desc


def test_catalog_mode_description_contains_available_tools_header():
    """Catalog mode description should have the standard header."""
    tools = _make_tools(6)
    out = _run(tools, _cfg(lazy_mode="catalog"))
    search_tool = next(t for t in out["tools"] if t["name"] == SEARCH_TOOL_NAME)
    assert "Available tools" in search_tool["description"]
    assert "select:" in search_tool["description"]


# --- minimal mode tests ---


def test_minimal_mode_sends_stubs_plus_search_tool():
    """Minimal mode should send tool stubs + the search meta-tool."""
    tools = _make_tools(10)
    out = _run(tools, _cfg(lazy_mode="minimal"))
    # 10 stubs + 1 search tool
    assert len(out["tools"]) == 11
    names = [t["name"] for t in out["tools"]]
    assert SEARCH_TOOL_NAME in names
    for i in range(10):
        assert f"tool_{i}" in names


def test_minimal_mode_does_not_embed_names_in_description():
    """Minimal mode search tool should have a simple description."""
    tools = _make_tools(10)
    out = _run(tools, _cfg(lazy_mode="minimal"))
    search_tool = next(t for t in out["tools"] if t["name"] == SEARCH_TOOL_NAME)
    assert "Available tools" not in search_tool["description"]


# --- search_only mode tests ---


def test_search_only_mode_sends_only_search_tool():
    """search_only should send just the search meta-tool with no names."""
    tools = _make_tools(10)
    out = _run(tools, _cfg(lazy_mode="search_only"))
    assert len(out["tools"]) == 1
    assert out["tools"][0]["name"] == SEARCH_TOOL_NAME
    assert "Available tools" not in out["tools"][0]["description"]


# --- threshold tests ---


def test_lazy_loading_skipped_below_threshold():
    """When tool count is below threshold, all tools should be sent as-is."""
    tools = _make_tools(3)
    cfg = _cfg(lazy_mode="catalog", min_tools=10)
    cfg.lazy_min_tokens = 99999  # also set token threshold high
    out = _run(tools, cfg)
    names = [t["name"] for t in out["tools"]]
    assert SEARCH_TOOL_NAME not in names
    assert len(out["tools"]) == 3


def test_lazy_loading_activates_at_threshold():
    """When tool count meets threshold, lazy loading should activate."""
    tools = _make_tools(10)
    out = _run(tools, _cfg(lazy_mode="catalog", min_tools=10))
    # 10 bare stubs + 1 search tool
    assert len(out["tools"]) == 11
    search_tool = next(t for t in out["tools"] if t["name"] == SEARCH_TOOL_NAME)
    assert "Available tools" in search_tool["description"]


# --- _build_search_tool_definition unit tests ---


def test_build_search_tool_without_names():
    """Without tool names, description should be the base description."""
    tool = _build_search_tool_definition()
    assert tool["name"] == SEARCH_TOOL_NAME
    assert "Available tools" not in tool["description"]
    assert "query" in tool["inputSchema"]["properties"]


def test_build_search_tool_with_names():
    """With tool names, description should embed them."""
    names = ["create_issue", "list_issues", "search_designs"]
    tool = _build_search_tool_definition(tool_names=names)
    assert "create_issue" in tool["description"]
    assert "list_issues" in tool["description"]
    assert "search_designs" in tool["description"]
    assert "Available tools" in tool["description"]


def test_build_search_tool_with_empty_list():
    """Empty tool names list should produce base description."""
    tool = _build_search_tool_definition(tool_names=[])
    assert "Available tools" not in tool["description"]


# --- token savings comparison ---


def test_catalog_mode_smaller_than_minimal():
    """Catalog mode output should be significantly smaller than minimal."""
    import json

    tools = _make_tools(20)
    catalog_out = _run(tools, _cfg(lazy_mode="catalog"))
    minimal_out = _run(tools, _cfg(lazy_mode="minimal"))
    catalog_size = len(json.dumps(catalog_out))
    minimal_size = len(json.dumps(minimal_out))
    assert catalog_size < minimal_size * 0.5, (
        f"Catalog ({catalog_size}B) should be <50% of minimal ({minimal_size}B)"
    )

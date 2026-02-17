"""Tests for lazy loading modes, including the catalog mode and enhanced minimal mode."""

from __future__ import annotations

import json

from ultra_lean_mcp_proxy.config import ProxyConfig
from ultra_lean_mcp_proxy.proxy import (
    SEARCH_TOOL_NAME,
    ProxyMetrics,
    _build_search_tool_definition,
    _handle_tools_list_result,
    _minimal_tool,
    _strip_schema_metadata,
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
    tools = _make_tools(20)
    catalog_out = _run(tools, _cfg(lazy_mode="catalog"))
    minimal_out = _run(tools, _cfg(lazy_mode="minimal"))
    catalog_size = len(json.dumps(catalog_out))
    minimal_size = len(json.dumps(minimal_out))
    assert catalog_size < minimal_size * 0.5, (
        f"Catalog ({catalog_size}B) should be <50% of minimal ({minimal_size}B)"
    )


def _make_rich_tools(n: int) -> list[dict]:
    """Generate tools with rich schemas that have things to strip."""
    return [
        {
            "name": f"tool_{i}",
            "description": f"Description for tool {i} that does something useful and important.",
            "inputSchema": {
                "type": "object",
                "title": f"Tool{i}Schema",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "param_a": {
                        "type": "string",
                        "description": "First parameter for the tool",
                        "title": "ParamA",
                        "examples": ["example1", "example2"],
                        "default": "example1",
                    },
                    "param_b": {
                        "type": "object",
                        "description": "Nested config object with several fields",
                        "title": "ParamB",
                        "additionalProperties": False,
                        "properties": {
                            "field_x": {
                                "type": "string",
                                "description": "Deeply nested description that should be stripped",
                                "title": "FieldX",
                                "examples": ["x1"],
                            },
                            "field_y": {
                                "type": "integer",
                                "description": "Another deeply nested description to strip",
                                "default": 42,
                            },
                        },
                        "required": ["field_x"],
                    },
                },
                "required": ["param_a"],
            },
        }
        for i in range(n)
    ]


def test_enhanced_minimal_between_full_and_catalog():
    """Enhanced minimal stubs should be smaller than full schemas but larger than catalog stubs."""
    tools = _make_rich_tools(20)
    catalog_out = _run(tools, _cfg(lazy_mode="catalog"))
    minimal_out = _run(tools, _cfg(lazy_mode="minimal"))
    # Compare only the tool stubs (exclude search meta-tool) for fair comparison
    catalog_stubs = [t for t in catalog_out["tools"] if t["name"] != SEARCH_TOOL_NAME]
    minimal_stubs = [t for t in minimal_out["tools"] if t["name"] != SEARCH_TOOL_NAME]
    full_size = len(json.dumps(tools))
    catalog_size = len(json.dumps(catalog_stubs))
    minimal_size = len(json.dumps(minimal_stubs))
    assert catalog_size < minimal_size < full_size, (
        f"Expected catalog ({catalog_size}) < minimal ({minimal_size}) < full ({full_size})"
    )


# --- enhanced minimal mode: _strip_schema_metadata unit tests ---


def test_strip_preserves_nested_object_properties():
    """Nested object properties should be recursively preserved."""
    schema = {
        "type": "object",
        "properties": {
            "format": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["pdf", "png", "jpg"]},
                    "quality": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["type"],
            },
        },
        "required": ["format"],
    }
    result = _strip_schema_metadata(schema, 0)
    assert result["type"] == "object"
    assert result["required"] == ["format"]
    fmt = result["properties"]["format"]
    assert fmt["type"] == "object"
    assert fmt["required"] == ["type"]
    assert fmt["properties"]["type"]["enum"] == ["pdf", "png", "jpg"]
    assert fmt["properties"]["quality"]["minimum"] == 1
    assert fmt["properties"]["quality"]["maximum"] == 100


def test_strip_preserves_required_arrays():
    """required arrays must survive stripping."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    }
    result = _strip_schema_metadata(schema, 0)
    assert result["required"] == ["a", "b"]


def test_strip_preserves_enum_values():
    """enum arrays must survive stripping."""
    schema = {"type": "string", "enum": ["pdf", "png", "svg"]}
    result = _strip_schema_metadata(schema, 0)
    assert result["enum"] == ["pdf", "png", "svg"]


def test_strip_preserves_format():
    """format string must survive stripping."""
    schema = {"type": "string", "format": "uri"}
    result = _strip_schema_metadata(schema, 0)
    assert result["format"] == "uri"


def test_strip_removes_descriptions_at_depth_gt_1():
    """Descriptions should be kept at depth 0-1 but stripped deeper."""
    schema = {
        "type": "object",
        "description": "Root description",
        "properties": {
            "child": {
                "type": "object",
                "description": "Depth 1 description",
                "properties": {
                    "grandchild": {
                        "type": "string",
                        "description": "Depth 2 description -- should be stripped",
                    }
                },
            }
        },
    }
    result = _strip_schema_metadata(schema, 0)
    assert "description" in result  # depth 0
    child = result["properties"]["child"]
    assert "description" in child  # depth 1
    grandchild = child["properties"]["grandchild"]
    assert "description" not in grandchild  # depth 2 stripped


def test_strip_removes_title_examples_default_additional_properties():
    """title, examples, default, additionalProperties, $schema, $id should all be stripped."""
    schema = {
        "type": "object",
        "title": "MySchema",
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "my-schema",
        "additionalProperties": False,
        "properties": {
            "field": {
                "type": "string",
                "title": "Field Title",
                "examples": ["foo", "bar"],
                "default": "foo",
            }
        },
    }
    result = _strip_schema_metadata(schema, 0)
    assert "title" not in result
    assert "$schema" not in result
    assert "$id" not in result
    assert "additionalProperties" not in result
    field = result["properties"]["field"]
    assert "title" not in field
    assert "examples" not in field
    assert "default" not in field


def test_strip_preserves_items_in_arrays():
    """Array items schema should be recursively preserved."""
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["id"],
        },
    }
    result = _strip_schema_metadata(schema, 0)
    assert result["type"] == "array"
    items = result["items"]
    assert items["type"] == "object"
    assert items["required"] == ["id"]
    assert items["properties"]["tags"]["items"]["type"] == "string"


def test_strip_preserves_anyof_oneof_allof():
    """anyOf, oneOf, allOf variants should be recursively preserved."""
    schema = {
        "anyOf": [
            {"type": "string", "enum": ["a", "b"]},
            {"type": "object", "properties": {"x": {"type": "integer"}}},
        ]
    }
    result = _strip_schema_metadata(schema, 0)
    assert len(result["anyOf"]) == 2
    assert result["anyOf"][0]["enum"] == ["a", "b"]
    assert result["anyOf"][1]["properties"]["x"]["type"] == "integer"


def test_minimal_tool_compresses_description():
    """minimalTool should compress descriptions."""
    tool = {
        "name": "my_tool",
        "description": "This is a really long description that should get compressed by the compress_description function for efficiency purposes.",
        "inputSchema": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
        },
    }
    result = _minimal_tool(tool)
    assert result["name"] == "my_tool"
    assert isinstance(result["description"], str)
    assert len(result["description"]) > 0
    assert result["inputSchema"]["type"] == "object"


def test_minimal_tool_preserves_nested_structure():
    """minimalTool should preserve full nested schema structure via stripSchemaMetadata."""
    tool = {
        "name": "export_design",
        "description": "Export a design to a file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "design_id": {"type": "string", "description": "The design ID"},
                "format": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["pdf", "png"]},
                    },
                    "required": ["type"],
                },
            },
            "required": ["design_id", "format"],
        },
    }
    result = _minimal_tool(tool)
    schema = result["inputSchema"]
    assert schema["required"] == ["design_id", "format"]
    fmt = schema["properties"]["format"]
    assert fmt["type"] == "object"
    assert fmt["required"] == ["type"]
    assert fmt["properties"]["type"]["enum"] == ["pdf", "png"]


# --- M1: depth 3+ description stripping ---


def test_strip_descriptions_at_depth_3_and_4():
    """Descriptions at depth 3+ should be stripped, while structure is preserved."""
    schema = {
        "type": "object",
        "description": "depth 0 - kept",
        "properties": {
            "l1": {
                "type": "object",
                "description": "depth 1 - kept",
                "properties": {
                    "l2": {
                        "type": "object",
                        "description": "depth 2 - stripped",
                        "properties": {
                            "l3": {
                                "type": "object",
                                "description": "depth 3 - stripped",
                                "properties": {
                                    "l4": {
                                        "type": "string",
                                        "description": "depth 4 - stripped",
                                    }
                                },
                                "required": ["l4"],
                            }
                        },
                    }
                },
            }
        },
    }
    result = _strip_schema_metadata(schema, 0)
    assert "description" in result  # depth 0
    l1 = result["properties"]["l1"]
    assert "description" in l1  # depth 1
    l2 = l1["properties"]["l2"]
    assert "description" not in l2  # depth 2
    assert l2["type"] == "object"
    l3 = l2["properties"]["l3"]
    assert "description" not in l3  # depth 3
    assert l3["required"] == ["l4"]
    l4 = l3["properties"]["l4"]
    assert "description" not in l4  # depth 4
    assert l4["type"] == "string"


# --- M2: array type values ---


def test_strip_preserves_array_type_values():
    """type: ["string", "null"] (nullable) should be preserved."""
    schema = {"type": ["string", "null"], "minLength": 1}
    result = _strip_schema_metadata(schema, 0)
    assert result["type"] == ["string", "null"]
    assert result["minLength"] == 1


# --- C1: const, pattern, not ---


def test_strip_preserves_const():
    """const values must survive stripping."""
    schema = {"const": "fixed_value"}
    result = _strip_schema_metadata(schema, 0)
    assert result["const"] == "fixed_value"


def test_strip_preserves_const_null():
    """const: null must survive stripping."""
    schema = {"const": None}
    result = _strip_schema_metadata(schema, 0)
    assert "const" in result
    assert result["const"] is None


def test_strip_preserves_pattern():
    """pattern strings must survive stripping."""
    schema = {"type": "string", "pattern": "^[A-Z]{2}$"}
    result = _strip_schema_metadata(schema, 0)
    assert result["pattern"] == "^[A-Z]{2}$"


def test_strip_preserves_not():
    """not combinator must be recursively preserved."""
    schema = {
        "not": {"type": "string", "enum": ["forbidden"]}
    }
    result = _strip_schema_metadata(schema, 0)
    assert result["not"]["type"] == "string"
    assert result["not"]["enum"] == ["forbidden"]


# --- M5: $ref ---


def test_strip_preserves_ref():
    """$ref strings must survive stripping."""
    schema = {"$ref": "#/definitions/User"}
    result = _strip_schema_metadata(schema, 0)
    assert result["$ref"] == "#/definitions/User"


# --- M4: validation keywords ---


def test_strip_preserves_min_max_length():
    """minLength/maxLength must survive stripping."""
    schema = {"type": "string", "minLength": 5, "maxLength": 100}
    result = _strip_schema_metadata(schema, 0)
    assert result["minLength"] == 5
    assert result["maxLength"] == 100


def test_strip_preserves_min_max_items():
    """minItems/maxItems must survive stripping."""
    schema = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10}
    result = _strip_schema_metadata(schema, 0)
    assert result["minItems"] == 1
    assert result["maxItems"] == 10


# --- H1: reference sharing ---


def test_strip_copies_required_array():
    """required array in output should be a copy, not a shared reference."""
    schema = {"type": "object", "required": ["a", "b"]}
    result = _strip_schema_metadata(schema, 0)
    result["required"].append("c")
    assert schema["required"] == ["a", "b"]  # original unchanged


def test_strip_copies_enum_array():
    """enum array in output should be a copy, not a shared reference."""
    schema = {"type": "string", "enum": ["x", "y"]}
    result = _strip_schema_metadata(schema, 0)
    result["enum"].append("z")
    assert schema["enum"] == ["x", "y"]  # original unchanged


# --- H2: items as array (tuple validation) ---


def test_strip_preserves_items_as_array():
    """items as array (tuple validation) should be recursively preserved."""
    schema = {
        "type": "array",
        "items": [
            {"type": "string"},
            {"type": "integer", "minimum": 0},
        ],
    }
    result = _strip_schema_metadata(schema, 0)
    assert isinstance(result["items"], list)
    assert len(result["items"]) == 2
    assert result["items"][0]["type"] == "string"
    assert result["items"][1]["type"] == "integer"
    assert result["items"][1]["minimum"] == 0


# --- C2: empty inputSchema {} ---


def test_minimal_tool_uses_empty_input_schema_dict():
    """Empty {} inputSchema should be used, not skipped to input_schema."""
    tool = {
        "name": "test",
        "description": "desc",
        "inputSchema": {},
        "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
    }
    result = _minimal_tool(tool)
    # Should use the empty inputSchema, not fall through to input_schema
    assert result["inputSchema"] == {}

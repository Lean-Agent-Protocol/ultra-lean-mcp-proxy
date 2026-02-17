"""Tests for tools_hash_sync hashing contract and proxy flow."""

from __future__ import annotations

import json
from pathlib import Path

from ultra_lean_mcp_proxy.config import ProxyConfig
from ultra_lean_mcp_proxy.proxy import ProxyMetrics, _client_supports_tools_hash_sync, _handle_tools_list_result
from ultra_lean_mcp_proxy.result_compression import TokenCounter
from ultra_lean_mcp_proxy.state import ProxyState, clone_json
from ultra_lean_mcp_proxy.tools_hash_sync import canonical_tools_json, compute_tools_hash, parse_if_none_match


def _sample_tools_result(version: int = 1) -> dict:
    tool_name = "list_items_v2" if version == 2 else "list_items"
    return {
        "tools": [
            {
                "name": tool_name,
                "description": "List items",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "page": {"type": "integer"},
                    },
                },
            }
        ]
    }


def _cfg(*, refresh_interval: int = 50) -> ProxyConfig:
    return ProxyConfig(
        definition_compression_enabled=False,
        lazy_loading_enabled=False,
        tools_hash_sync_enabled=True,
        tools_hash_sync_algorithm="sha256",
        tools_hash_sync_refresh_interval=refresh_interval,
        tools_hash_sync_include_server_fingerprint=False,
    )


def _run_tools_list(
    *,
    result: dict,
    state: ProxyState,
    config: ProxyConfig,
    metrics: ProxyMetrics,
    counter: TokenCounter,
    negotiated: bool,
    if_none_match: str | None = None,
    provided: bool = False,
    valid: bool = False,
) -> dict:
    return _handle_tools_list_result(
        clone_json(result),
        state,
        config,
        metrics,
        counter,
        tools_hash_sync_negotiated=negotiated,
        profile_fingerprint="profile-a",
        if_none_match=if_none_match,
        if_none_match_provided=provided,
        if_none_match_valid=valid,
    )


def test_tools_hash_canonicalization_stable_for_key_order():
    tools_a = [{"name": "x", "inputSchema": {"type": "object", "properties": {"a": {"type": "string"}}}}]
    tools_b = [{"inputSchema": {"properties": {"a": {"type": "string"}}, "type": "object"}, "name": "x"}]
    assert canonical_tools_json(tools_a) == canonical_tools_json(tools_b)
    assert compute_tools_hash(tools_a) == compute_tools_hash(tools_b)


def test_tools_hash_fixture_contract_matches_expected_wire_format():
    fixture_path = Path(__file__).parent / "fixtures" / "v2_tools_list_sample.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    wire = compute_tools_hash(payload["tools"])
    assert wire.startswith("sha256:")
    assert len(wire) == len("sha256:") + 64


def test_tools_hash_server_fingerprint_binding_changes_hash():
    tools = _sample_tools_result()["tools"]
    plain = compute_tools_hash(tools, include_server_fingerprint=False)
    bound_a = compute_tools_hash(tools, include_server_fingerprint=True, server_fingerprint="srv-a")
    bound_b = compute_tools_hash(tools, include_server_fingerprint=True, server_fingerprint="srv-b")
    assert plain != bound_a
    assert bound_a != bound_b


def test_parse_if_none_match_contract():
    valid = "sha256:" + ("a" * 64)
    assert parse_if_none_match(valid) == valid
    assert parse_if_none_match(valid.upper()) == valid
    assert parse_if_none_match("sha1:" + ("a" * 64)) is None
    assert parse_if_none_match("sha256:zzzz") is None
    assert parse_if_none_match(123) is None


def test_client_capability_handshake_detection():
    assert _client_supports_tools_hash_sync(
        {
            "capabilities": {
                "experimental": {
                    "ultra_lean_mcp_proxy": {
                        "tools_hash_sync": {"version": 1},
                    }
                }
            }
        }
    )
    assert not _client_supports_tools_hash_sync({"capabilities": {}})


def test_tools_hash_sync_unsupported_client_gets_full_tools():
    state = ProxyState(max_cache_entries=32)
    metrics = ProxyMetrics()
    counter = TokenCounter()
    cfg = _cfg()
    result = _sample_tools_result()
    etag = compute_tools_hash(result["tools"])

    out = _run_tools_list(
        result=result,
        state=state,
        config=cfg,
        metrics=metrics,
        counter=counter,
        negotiated=False,
        if_none_match=etag,
        provided=True,
        valid=True,
    )
    assert out["tools"]
    assert "_ultra_lean_mcp_proxy" not in out or "tools_hash_sync" not in out.get("_ultra_lean_mcp_proxy", {})


def test_tools_hash_sync_supported_matching_hash_returns_not_modified():
    state = ProxyState(max_cache_entries=32)
    metrics = ProxyMetrics()
    counter = TokenCounter()
    cfg = _cfg()
    result = _sample_tools_result()

    first = _run_tools_list(
        result=result,
        state=state,
        config=cfg,
        metrics=metrics,
        counter=counter,
        negotiated=True,
    )
    tools_hash = first["_ultra_lean_mcp_proxy"]["tools_hash_sync"]["tools_hash"]

    second = _run_tools_list(
        result=result,
        state=state,
        config=cfg,
        metrics=metrics,
        counter=counter,
        negotiated=True,
        if_none_match=tools_hash,
        provided=True,
        valid=True,
    )
    assert second["tools"] == []
    assert second["_ultra_lean_mcp_proxy"]["tools_hash_sync"]["not_modified"] is True
    assert second["_ultra_lean_mcp_proxy"]["tools_hash_sync"]["tools_hash"] == tools_hash
    assert metrics.tools_hash_sync_hits == 1
    assert metrics.tools_hash_sync_not_modified == 1


def test_tools_hash_sync_changed_snapshot_returns_full_tools():
    state = ProxyState(max_cache_entries=32)
    metrics = ProxyMetrics()
    counter = TokenCounter()
    cfg = _cfg()

    first = _run_tools_list(
        result=_sample_tools_result(version=1),
        state=state,
        config=cfg,
        metrics=metrics,
        counter=counter,
        negotiated=True,
    )
    old_hash = first["_ultra_lean_mcp_proxy"]["tools_hash_sync"]["tools_hash"]

    second = _run_tools_list(
        result=_sample_tools_result(version=2),
        state=state,
        config=cfg,
        metrics=metrics,
        counter=counter,
        negotiated=True,
        if_none_match=old_hash,
        provided=True,
        valid=True,
    )
    assert second["tools"]
    assert second["_ultra_lean_mcp_proxy"]["tools_hash_sync"]["not_modified"] is False
    assert second["_ultra_lean_mcp_proxy"]["tools_hash_sync"]["tools_hash"] != old_hash
    assert metrics.tools_hash_sync_misses == 1


def test_tools_hash_sync_periodic_forced_refresh_returns_full_snapshot():
    state = ProxyState(max_cache_entries=32)
    metrics = ProxyMetrics()
    counter = TokenCounter()
    cfg = _cfg(refresh_interval=2)
    result = _sample_tools_result()

    first = _run_tools_list(
        result=result,
        state=state,
        config=cfg,
        metrics=metrics,
        counter=counter,
        negotiated=True,
    )
    tools_hash = first["_ultra_lean_mcp_proxy"]["tools_hash_sync"]["tools_hash"]

    second = _run_tools_list(
        result=result,
        state=state,
        config=cfg,
        metrics=metrics,
        counter=counter,
        negotiated=True,
        if_none_match=tools_hash,
        provided=True,
        valid=True,
    )
    assert second["_ultra_lean_mcp_proxy"]["tools_hash_sync"]["not_modified"] is True

    third = _run_tools_list(
        result=result,
        state=state,
        config=cfg,
        metrics=metrics,
        counter=counter,
        negotiated=True,
        if_none_match=tools_hash,
        provided=True,
        valid=True,
    )
    assert third["tools"]
    assert third["_ultra_lean_mcp_proxy"]["tools_hash_sync"]["not_modified"] is False
    assert metrics.tools_hash_sync_hits == 2
    assert metrics.tools_hash_sync_not_modified == 1


def test_tools_hash_sync_malformed_if_none_match_fails_open_to_full():
    state = ProxyState(max_cache_entries=32)
    metrics = ProxyMetrics()
    counter = TokenCounter()
    cfg = _cfg()
    result = _sample_tools_result()

    out = _run_tools_list(
        result=result,
        state=state,
        config=cfg,
        metrics=metrics,
        counter=counter,
        negotiated=True,
        if_none_match=None,
        provided=True,
        valid=False,
    )
    assert out["tools"]
    assert out["_ultra_lean_mcp_proxy"]["tools_hash_sync"]["not_modified"] is False


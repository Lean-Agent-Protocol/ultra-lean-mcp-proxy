"""Ultra Lean MCP Proxy with composable optimization pipeline (v2)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Optional

from .compress import compress_description, compress_schema
from .config import ProxyConfig
from .delta import create_delta, stable_hash
from .result_compression import (
    CompressionOptions,
    TokenCounter,
    compress_result,
    estimate_compressibility,
    token_savings,
)
from .state import ProxyState, clone_json, is_mutating_tool_name, make_cache_key
from .tools_hash_sync import compute_tools_hash, parse_if_none_match

logger = logging.getLogger("ultra_lean_mcp_proxy.proxy")

SEARCH_TOOL_NAME = "ultra_lean_mcp_proxy.search_tools"
STDIO_STREAM_LIMIT = 8 * 1024 * 1024


@dataclass
class PendingRequest:
    method: str
    tool_name: Optional[str] = None
    arguments: Any = None
    cache_key: Optional[str] = None
    tools_hash_if_none_match: Optional[str] = None
    tools_hash_if_none_match_provided: bool = False
    tools_hash_if_none_match_valid: bool = False
    client_tools_hash_sync_supported: bool = False


@dataclass
class ProxyMetrics:
    tools_list_requests: int = 0
    tools_list_saved_bytes: int = 0
    tools_hash_sync_hits: int = 0
    tools_hash_sync_misses: int = 0
    tools_hash_sync_not_modified: int = 0
    tools_hash_sync_saved_bytes: int = 0
    tools_hash_sync_saved_tokens: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    result_compressions: int = 0
    result_saved_bytes: int = 0
    delta_responses: int = 0
    delta_saved_bytes: int = 0
    search_calls: int = 0
    upstream_requests: int = 0
    upstream_request_bytes: int = 0
    upstream_request_tokens: int = 0
    upstream_responses: int = 0
    upstream_response_bytes: int = 0
    upstream_response_tokens: int = 0


@dataclass
class FeatureHealth:
    regression_streak: int = 0
    cooldown_remaining: int = 0


def _runtime_metrics_snapshot(metrics: ProxyMetrics) -> dict[str, int]:
    return {
        "upstream_requests": int(metrics.upstream_requests),
        "upstream_request_tokens": int(metrics.upstream_request_tokens),
        "upstream_request_bytes": int(metrics.upstream_request_bytes),
        "upstream_responses": int(metrics.upstream_responses),
        "upstream_response_tokens": int(metrics.upstream_response_tokens),
        "upstream_response_bytes": int(metrics.upstream_response_bytes),
    }


def _feature_health_key(feature: str, tool_name: Optional[str]) -> str:
    return f"{feature}:{tool_name or '_global'}"


def _feature_is_active(feature_states: dict[str, FeatureHealth], key: str, config: ProxyConfig) -> bool:
    if not config.auto_disable_enabled:
        return True
    state = feature_states.setdefault(key, FeatureHealth())
    if state.cooldown_remaining > 0:
        state.cooldown_remaining -= 1
        return False
    return True


def _record_feature_outcome(
    feature_states: dict[str, FeatureHealth],
    key: str,
    *,
    outcome: str,
    config: ProxyConfig,
):
    if not config.auto_disable_enabled:
        return
    state = feature_states.setdefault(key, FeatureHealth())
    if outcome == "success":
        state.regression_streak = 0
        return
    if outcome == "neutral":
        state.regression_streak = max(0, state.regression_streak - 1)
        return
    if outcome == "hurt":
        state.regression_streak += 1
        if state.regression_streak >= config.auto_disable_threshold:
            state.regression_streak = 0
            state.cooldown_remaining = config.auto_disable_cooldown_requests


async def _read_jsonrpc(reader: asyncio.StreamReader) -> Optional[dict]:
    """Read one newline-delimited JSON-RPC message."""
    while True:
        line = await reader.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping non-JSON line: %s", line[:100])
            continue


def _write_jsonrpc(writer: asyncio.StreamWriter, msg: dict):
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n"
    writer.write(data.encode("utf-8"))


def _write_jsonrpc_stdout(msg: dict):
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(data.encode("utf-8"))
    sys.stdout.buffer.flush()


def _read_jsonrpc_stdin_sync() -> Optional[dict]:
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping non-JSON line from stdin: %s", line[:100])
            continue


def _json_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _resolve_upstream_command(command: list[str]) -> list[str]:
    if not command:
        return command
    first = command[0]
    resolved = shutil.which(first)
    if not resolved and os.name == "nt" and not first.lower().endswith(".cmd"):
        resolved = shutil.which(f"{first}.cmd")
    if resolved:
        return [resolved, *command[1:]]
    return command


def _extract_tool_call(msg: dict) -> tuple[Optional[str], dict]:
    params = msg.get("params", {})
    if not isinstance(params, dict):
        return None, {}
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}
    return (str(name) if isinstance(name, str) else None), arguments


def _client_supports_tools_hash_sync(params: Any) -> bool:
    if not isinstance(params, dict):
        return False
    caps = params.get("capabilities")
    if not isinstance(caps, dict):
        return False
    experimental = caps.get("experimental")
    if not isinstance(experimental, dict):
        return False
    proxy_ext = experimental.get("ultra_lean_mcp_proxy")
    if not isinstance(proxy_ext, dict):
        return False
    tools_hash_sync = proxy_ext.get("tools_hash_sync")
    if not isinstance(tools_hash_sync, dict):
        return False
    version = tools_hash_sync.get("version")
    if isinstance(version, int):
        return version == 1
    if isinstance(version, str):
        return version.strip() == "1"
    return False


def _extract_tools_hash_if_none_match(
    params: Any,
    *,
    algorithm: str,
) -> tuple[bool, bool, Optional[str]]:
    """Return (provided, valid, normalized_value)."""
    if not isinstance(params, dict):
        return False, False, None
    proxy_ext = params.get("_ultra_lean_mcp_proxy")
    if not isinstance(proxy_ext, dict):
        return False, False, None
    tools_hash_sync = proxy_ext.get("tools_hash_sync")
    if not isinstance(tools_hash_sync, dict):
        return False, False, None
    if_none_match = tools_hash_sync.get("if_none_match")
    if if_none_match is None:
        return False, False, None
    normalized = parse_if_none_match(if_none_match, expected_algorithm=algorithm)
    if normalized is None:
        return True, False, None
    return True, True, normalized


def _inject_initialize_tools_hash_capability(
    result: Any,
    *,
    algorithm: str,
) -> Any:
    if not isinstance(result, dict):
        return result
    out = clone_json(result)
    caps = out.setdefault("capabilities", {})
    if not isinstance(caps, dict):
        return result
    experimental = caps.setdefault("experimental", {})
    if not isinstance(experimental, dict):
        return result
    proxy_ext = experimental.setdefault("ultra_lean_mcp_proxy", {})
    if not isinstance(proxy_ext, dict):
        return result
    proxy_ext["tools_hash_sync"] = {
        "version": 1,
        "algorithm": algorithm,
    }
    return out


def _tools_hash_scope_key(config: ProxyConfig, profile_fingerprint: str) -> str:
    return f"{config.session_id}:{config.server_name}:{profile_fingerprint}"


def _build_profile_fingerprint(config: ProxyConfig, upstream_command: list[str]) -> str:
    payload = {
        "server_name": config.server_name,
        "command": " ".join(upstream_command),
    }
    return stable_hash(payload)


def _build_search_tool_definition(tool_names: list[str] | None = None) -> dict[str, Any]:
    base_desc = "Search available tools and return full schemas on demand."
    if tool_names:
        name_list = "\n".join(tool_names)
        description = (
            base_desc
            + ' Use "select:<tool_name>" for direct selection, or keywords to search.\n\n'
            "Available tools (must be loaded via this tool before use):\n"
            + name_list
        )
    else:
        description = base_desc
    return {
        "name": SEARCH_TOOL_NAME,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "server": {"type": "string", "description": "Optional server name"},
                "top_k": {"type": "integer", "description": "Max number of results", "default": 8},
                "include_schemas": {
                    "type": "boolean",
                    "description": "Include inputSchema in matches",
                    "default": False,
                },
            },
            "required": ["query"],
        },
    }


def _apply_definition_compression(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for tool in tools:
        item = clone_json(tool)
        if "description" in item:
            item["description"] = compress_description(str(item["description"]))
        schema = item.get("inputSchema") or item.get("input_schema")
        if isinstance(schema, dict):
            compress_schema(schema)
        out.append(item)
    return out


def _strip_schema_metadata(schema: Any, depth: int = 0) -> Any:
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    if "type" in schema:
        out["type"] = schema["type"]
    req = schema.get("required")
    if isinstance(req, list) and req:
        out["required"] = list(req)
    if isinstance(schema.get("enum"), list):
        out["enum"] = list(schema["enum"])
    if isinstance(schema.get("format"), str):
        out["format"] = schema["format"]
    if isinstance(schema.get("pattern"), str):
        out["pattern"] = schema["pattern"]
    if "const" in schema:
        out["const"] = schema["const"]
    if isinstance(schema.get("$ref"), str):
        out["$ref"] = schema["$ref"]
    if isinstance(schema.get("minimum"), (int, float)):
        out["minimum"] = schema["minimum"]
    if isinstance(schema.get("maximum"), (int, float)):
        out["maximum"] = schema["maximum"]
    if isinstance(schema.get("minLength"), (int, float)):
        out["minLength"] = schema["minLength"]
    if isinstance(schema.get("maxLength"), (int, float)):
        out["maxLength"] = schema["maxLength"]
    if isinstance(schema.get("minItems"), (int, float)):
        out["minItems"] = schema["minItems"]
    if isinstance(schema.get("maxItems"), (int, float)):
        out["maxItems"] = schema["maxItems"]
    if isinstance(schema.get("description"), str) and depth <= 1:
        out["description"] = compress_description(schema["description"])
    props = schema.get("properties")
    if isinstance(props, dict):
        out["properties"] = {
            k: _strip_schema_metadata(v, depth + 1) for k, v in props.items()
        }
    items = schema.get("items")
    if isinstance(items, list):
        out["items"] = [_strip_schema_metadata(s, depth + 1) for s in items]
    elif isinstance(items, dict):
        out["items"] = _strip_schema_metadata(items, depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            out[key] = [_strip_schema_metadata(s, depth + 1) for s in variants]
    not_schema = schema.get("not")
    if isinstance(not_schema, dict):
        out["not"] = _strip_schema_metadata(not_schema, depth + 1)
    return out


def _minimal_tool(tool: dict[str, Any]) -> dict[str, Any]:
    name = tool.get("name", "")
    description = tool.get("description", "")
    schema = tool.get("inputSchema")
    if schema is None:
        schema = tool.get("input_schema")
    if schema is None:
        schema = {}
    return {
        "name": name,
        "description": compress_description(description) or description,
        "inputSchema": _strip_schema_metadata(schema, 0),
    }


def _handle_tools_list_result(
    result: dict[str, Any],
    state: ProxyState,
    config: ProxyConfig,
    metrics: ProxyMetrics,
    token_counter: TokenCounter,
    *,
    tools_hash_sync_negotiated: bool,
    profile_fingerprint: str,
    if_none_match: Optional[str] = None,
    if_none_match_provided: bool = False,
    if_none_match_valid: bool = False,
) -> dict[str, Any]:
    tools = result.get("tools", [])
    if not isinstance(tools, list):
        return result

    metrics.tools_list_requests += 1
    original_size = _json_size(result)

    processed_tools = clone_json(tools)
    if config.definition_compression_enabled:
        processed_tools = _apply_definition_compression(processed_tools)

    # Index toolset for lazy search meta-tool.
    state.set_tools(processed_tools)

    visible_tools = processed_tools
    if config.lazy_loading_enabled:
        tool_count = len(processed_tools)
        tool_tokens = token_counter.count({"tools": processed_tools})
        lazy_allowed = tool_count >= config.lazy_min_tools or tool_tokens >= config.lazy_min_tokens
    else:
        lazy_allowed = False

    if lazy_allowed:
        if config.lazy_mode == "search_only":
            visible_tools = []
        elif config.lazy_mode == "catalog":
            visible_tools = [
                {"name": t.get("name", ""), "inputSchema": {"type": "object"}}
                for t in processed_tools
            ]
        elif config.lazy_mode == "minimal":
            visible_tools = [_minimal_tool(t) for t in processed_tools]
        tool_names = (
            [t.get("name", "") for t in processed_tools]
            if config.lazy_mode == "catalog"
            else None
        )
        visible_tools.append(_build_search_tool_definition(tool_names))

    out = clone_json(result)
    out["tools"] = visible_tools
    compressed_size = _json_size(out)
    saved = original_size - compressed_size
    if saved > 0:
        metrics.tools_list_saved_bytes += saved

    if not (config.tools_hash_sync_enabled and tools_hash_sync_negotiated):
        return out

    scope_key = _tools_hash_scope_key(config, profile_fingerprint)
    try:
        tools_hash = compute_tools_hash(
            visible_tools,
            algorithm=config.tools_hash_sync_algorithm,
            include_server_fingerprint=config.tools_hash_sync_include_server_fingerprint,
            server_fingerprint=profile_fingerprint,
        )
        state.tools_hash_set_last(scope_key, tools_hash)

        conditional_match = bool(if_none_match_valid and if_none_match == tools_hash)
        if conditional_match:
            hit_count = state.tools_hash_record_hit(scope_key)
            metrics.tools_hash_sync_hits += 1
            force_refresh = (hit_count % config.tools_hash_sync_refresh_interval) == 0
            if not force_refresh:
                not_modified = clone_json(out)
                not_modified["tools"] = []
                ext = not_modified.setdefault("_ultra_lean_mcp_proxy", {})
                if not isinstance(ext, dict):
                    ext = {}
                    not_modified["_ultra_lean_mcp_proxy"] = ext
                ext["tools_hash_sync"] = {
                    "not_modified": True,
                    "tools_hash": tools_hash,
                }

                metrics.tools_hash_sync_not_modified += 1
                byte_delta = max(0, _json_size(out) - _json_size(not_modified))
                if byte_delta > 0:
                    metrics.tools_hash_sync_saved_bytes += byte_delta
                token_delta = max(0, token_counter.count(out) - token_counter.count(not_modified))
                if token_delta > 0:
                    metrics.tools_hash_sync_saved_tokens += token_delta
                return not_modified
        else:
            if if_none_match_provided and if_none_match_valid:
                metrics.tools_hash_sync_misses += 1

        state.tools_hash_reset_hits(scope_key)
        ext = out.setdefault("_ultra_lean_mcp_proxy", {})
        if isinstance(ext, dict):
            ext["tools_hash_sync"] = {
                "not_modified": False,
                "tools_hash": tools_hash,
            }
        return out
    except Exception as exc:
        logger.debug("tools_hash_sync skipped due to error (fail-open): %s", exc)
    return out


def _build_search_result(
    state: ProxyState,
    config: ProxyConfig,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    query = str(arguments.get("query", "")).strip()
    top_k = arguments.get("top_k", config.lazy_top_k)
    include_schemas = arguments.get("include_schemas", False)
    try:
        top_k_int = max(1, int(top_k))
    except (TypeError, ValueError):
        top_k_int = config.lazy_top_k
    include = bool(include_schemas)

    matches = state.search_tools(query=query, top_k=top_k_int, include_schemas=include)
    top_score = float(matches[0].get("score", 0.0)) if matches else 0.0
    payload = {
        "server": config.server_name,
        "query": query,
        "count": len(matches),
        "matches": matches,
    }
    if (
        config.lazy_fallback_full_on_low_confidence
        and top_score < config.lazy_min_confidence_score
    ):
        payload["fallback"] = "full_tools_due_low_confidence"
        payload["top_score"] = top_score
        payload["tools"] = state.get_tools()

    return {
        "structuredContent": payload,
        "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}],
    }


def _tool_cache_allowed(config: ProxyConfig, tool_name: Optional[str]) -> bool:
    if not tool_name or not config.caching_enabled:
        return False
    if not config.feature_enabled_for_tool(tool_name, "caching", True):
        return False
    if not config.cache_mutating_tools and is_mutating_tool_name(tool_name):
        return False
    return True


def _minify_redundant_text_content(content: list[Any], original_payload: Any) -> tuple[list[Any], bool]:
    """Drop text items that redundantly embed the same JSON payload."""
    kept: list[Any] = []
    removed = 0
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            kept.append(item)
            continue
        text = item.get("text")
        if not isinstance(text, str):
            kept.append(item)
            continue
        parsed = None
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
        if parsed == original_payload:
            removed += 1
            continue
        kept.append(item)
    if removed > 0 and not kept:
        kept = [{"type": "text", "text": "[ultra-lean-mcp-proxy] structured result"}]
    return kept, removed > 0


def _apply_result_compression(
    result: Any,
    tool_name: Optional[str],
    config: ProxyConfig,
    metrics: ProxyMetrics,
    token_counter: TokenCounter,
    feature_states: dict[str, FeatureHealth],
    key_registry: dict[str, dict[str, str]],
    key_registry_counter: dict[str, int],
) -> Any:
    if not config.result_compression_enabled:
        return result
    if not config.feature_enabled_for_tool(tool_name, "result_compression", True):
        return result
    feature_key = _feature_health_key("result_compression", tool_name)
    if not _feature_is_active(feature_states, feature_key, config):
        return result

    options = CompressionOptions(
        mode=config.result_compression_mode,
        strip_nulls=config.result_strip_nulls,
        strip_defaults=config.result_strip_defaults,
        min_payload_bytes=config.result_min_payload_bytes,
    )
    outcome = "neutral"

    try:
        # Preferred target: structured content field.
        if isinstance(result, dict) and isinstance(result.get("structuredContent"), (dict, list)):
            out = clone_json(result)
            original = out["structuredContent"]
            if estimate_compressibility(original) < config.result_min_compressibility:
                _record_feature_outcome(feature_states, feature_key, outcome="neutral", config=config)
                return result
            env = compress_result(
                original,
                options,
                key_registry=key_registry,
                registry_counter=key_registry_counter,
                reuse_keys=config.result_shared_key_registry,
                key_bootstrap_interval=config.result_key_bootstrap_interval,
            )
            if env.get("compressed"):
                token_delta = token_savings(original, env, token_counter)
                min_required = max(
                    config.result_min_token_savings_abs,
                    int(token_counter.count(original) * config.result_min_token_savings_ratio),
                )
                if token_delta >= min_required:
                    out["structuredContent"] = env
                    out.setdefault("_ultra_lean_mcp_proxy", {})["result_compression"] = {
                        "saved_bytes": env.get("savedBytes", 0),
                        "saved_ratio": env.get("savedRatio", 0.0),
                        "saved_tokens": token_delta,
                    }
                    metrics.result_compressions += 1
                    metrics.result_saved_bytes += int(env.get("savedBytes", 0))
                    outcome = "success"
                    if config.result_minify_redundant_text and isinstance(out.get("content"), list):
                        new_content, changed = _minify_redundant_text_content(out["content"], original)
                        if changed:
                            out["content"] = new_content
                elif token_delta < 0:
                    outcome = "hurt"
            _record_feature_outcome(feature_states, feature_key, outcome=outcome, config=config)
            if outcome == "success":
                return out
            return result

        # Fallback: text content containing JSON payload.
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            out = clone_json(result)
            changed = False
            total_saved = 0
            total_saved_tokens = 0
            for item in out["content"]:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                text = item.get("text")
                if not isinstance(text, str):
                    continue
                stripped = text.strip()
                if not stripped.startswith("{") and not stripped.startswith("["):
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if estimate_compressibility(parsed) < config.result_min_compressibility:
                    continue
                env = compress_result(
                    parsed,
                    options,
                    key_registry=key_registry,
                    registry_counter=key_registry_counter,
                    reuse_keys=config.result_shared_key_registry,
                    key_bootstrap_interval=config.result_key_bootstrap_interval,
                )
                if not env.get("compressed"):
                    continue
                token_delta = token_savings(parsed, env, token_counter)
                min_required = max(
                    config.result_min_token_savings_abs,
                    int(token_counter.count(parsed) * config.result_min_token_savings_ratio),
                )
                if token_delta >= min_required:
                    item["text"] = json.dumps(env, separators=(",", ":"), ensure_ascii=False)
                    changed = True
                    total_saved += int(env.get("savedBytes", 0))
                    total_saved_tokens += token_delta
                    outcome = "success"
                elif token_delta < 0 and outcome != "success":
                    outcome = "hurt"
            if changed:
                out.setdefault("_ultra_lean_mcp_proxy", {})["result_compression"] = {
                    "saved_bytes": total_saved,
                    "saved_tokens": total_saved_tokens,
                }
                metrics.result_compressions += 1
                metrics.result_saved_bytes += total_saved
                _record_feature_outcome(feature_states, feature_key, outcome="success", config=config)
                return out
            _record_feature_outcome(feature_states, feature_key, outcome=outcome, config=config)
            return result
    except Exception as exc:
        logger.debug("result compression skipped due to error: %s", exc)
        _record_feature_outcome(feature_states, feature_key, outcome="neutral", config=config)
        return result

    _record_feature_outcome(feature_states, feature_key, outcome="neutral", config=config)
    return result


def _apply_delta_response(
    result: Any,
    history_key: str,
    tool_name: Optional[str],
    state: ProxyState,
    config: ProxyConfig,
    metrics: ProxyMetrics,
    delta_counters: dict[str, int],
    token_counter: TokenCounter,
) -> Any:
    previous = state.history_get(history_key)
    state.history_set(history_key, result)

    if not config.delta_responses_enabled:
        return result
    if not config.feature_enabled_for_tool(tool_name, "delta_responses", True):
        return result
    if previous is None:
        delta_counters[history_key] = 0
        return result
    if delta_counters.get(history_key, 0) >= config.delta_snapshot_interval:
        delta_counters[history_key] = 0
        return result

    full_tokens = token_counter.count(result)

    if previous == result:
        delta = {
            "encoding": "lapc-delta-v1",
            "unchanged": True,
            "currentHash": stable_hash(result),
        }
        payload = {"delta": delta}
        if token_counter.count(payload) >= full_tokens:
            return result
        delta_counters[history_key] = delta_counters.get(history_key, 0) + 1
        metrics.delta_responses += 1
        metrics.delta_saved_bytes += max(0, _json_size(result) - _json_size(payload))
        return {
            "structuredContent": payload,
            "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}],
        }

    try:
        delta = create_delta(
            previous=previous,
            current=result,
            min_savings_ratio=config.delta_min_savings_ratio,
            max_patch_bytes=config.delta_max_patch_bytes,
        )
        if not delta:
            return result
        patch_ratio = (
            (float(delta.get("patchBytes", 0)) / float(delta.get("fullBytes", 1)))
            if float(delta.get("fullBytes", 0)) > 0
            else 0.0
        )
        if patch_ratio > config.delta_max_patch_ratio:
            return result
        payload = {"delta": delta}
        if token_counter.count(payload) >= full_tokens:
            return result
        delta_counters[history_key] = delta_counters.get(history_key, 0) + 1
        metrics.delta_responses += 1
        metrics.delta_saved_bytes += int(delta.get("savedBytes", 0))
        return {
            "structuredContent": payload,
            "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}],
        }
    except Exception as exc:
        logger.debug("delta response skipped due to error: %s", exc)
        return result


def _trace_inbound(trace_rpc: bool, msg: dict) -> None:
    """Log inbound client JSON-RPC messages to stderr."""
    if not trace_rpc:
        return
    method = msg.get("method")
    if method:
        id_part = f" id={msg['id']}" if msg.get("id") is not None else ""
        kind = "request" if msg.get("id") is not None else "notification"
        sys.stderr.write(f"[ultra-lean-mcp-proxy] rpc-> {kind} {method}{id_part}\n")
        sys.stderr.flush()


def _trace_upstream(trace_rpc: bool, msg: dict, pending: dict) -> None:
    """Log upstream JSON-RPC messages to stderr."""
    if not trace_rpc:
        return
    method = msg.get("method")
    if method:
        id_part = f" id={msg['id']}" if msg.get("id") is not None else ""
        kind = "request" if msg.get("id") is not None else "notification"
        sys.stderr.write(f"[ultra-lean-mcp-proxy] rpc<- upstream {kind} {method}{id_part}\n")
        sys.stderr.flush()
    elif msg.get("id") is not None:
        req_id = msg["id"]
        req = pending.get(req_id)
        origin = req.method if req else "?"
        status = "result" if "result" in msg else "error" if "error" in msg else "?"
        sys.stderr.write(f"[ultra-lean-mcp-proxy] rpc<- upstream response id={req_id} for={origin} status={status}\n")
        sys.stderr.flush()


async def run_proxy(command: list[str], config: Optional[ProxyConfig] = None, stats: bool = False):
    """Run Ultra Lean MCP Proxy with optional v2 optimizations."""
    cfg = config or ProxyConfig()
    if stats:
        cfg.stats = True

    command = _resolve_upstream_command(command)
    profile_fingerprint = _build_profile_fingerprint(cfg, command)

    logger.info("Starting upstream server: %s", command)

    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STDIO_STREAM_LIMIT,
    )

    upstream_stdin = proc.stdin
    upstream_stdout = proc.stdout

    trace_rpc = cfg.trace_rpc

    state = ProxyState(max_cache_entries=cfg.cache_max_entries)
    metrics = ProxyMetrics()
    token_counter = TokenCounter()
    feature_states: dict[str, FeatureHealth] = {}
    delta_counters: dict[str, int] = {}
    key_registry: dict[str, dict[str, str]] = {}
    key_registry_counter: dict[str, int] = {}
    pending: dict[Any, PendingRequest] = {}
    tools_hash_sync_negotiated = False
    client_write_lock = asyncio.Lock()

    if trace_rpc:
        sys.stderr.write("[ultra-lean-mcp-proxy] trace-rpc enabled\n")
        sys.stderr.flush()

    async def send_to_client(msg: dict):
        if cfg.stats and isinstance(msg, dict):
            result = msg.get("result")
            if isinstance(result, dict):
                proxy_ext = result.get("_ultra_lean_mcp_proxy")
                if not isinstance(proxy_ext, dict):
                    proxy_ext = {}
                    result["_ultra_lean_mcp_proxy"] = proxy_ext
                proxy_ext["runtime_metrics"] = _runtime_metrics_snapshot(metrics)
        async with client_write_lock:
            await asyncio.to_thread(_write_jsonrpc_stdout, msg)

    async def client_to_upstream():
        try:
            while True:
                msg = await asyncio.to_thread(_read_jsonrpc_stdin_sync)
                if msg is None:
                    logger.info("Client EOF, shutting down upstream")
                    upstream_stdin.close()
                    if proc.returncode is None:
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=0.5)
                        except asyncio.TimeoutError:
                            proc.terminate()
                            try:
                                await asyncio.wait_for(proc.wait(), timeout=2.0)
                            except asyncio.TimeoutError:
                                proc.kill()
                                await proc.wait()
                    return

                _trace_inbound(trace_rpc, msg)

                # Intercept JSON-RPC requests with id.
                method = msg.get("method")
                req_id = msg.get("id")
                if isinstance(method, str) and req_id is not None:
                    try:
                        if method == "initialize":
                            pending[req_id] = PendingRequest(
                                method=method,
                                client_tools_hash_sync_supported=_client_supports_tools_hash_sync(msg.get("params")),
                            )
                        elif method == "tools/list":
                            provided, valid, value = _extract_tools_hash_if_none_match(
                                msg.get("params"),
                                algorithm=cfg.tools_hash_sync_algorithm,
                            )
                            if (
                                cfg.tools_hash_sync_enabled
                                and tools_hash_sync_negotiated
                                and valid
                                and isinstance(value, str)
                            ):
                                scope_key = _tools_hash_scope_key(cfg, profile_fingerprint)
                                entry = state.tools_hash_get(scope_key)
                                if entry and entry.last_hash == value:
                                    next_hit = entry.conditional_hits + 1
                                    force_refresh = (next_hit % cfg.tools_hash_sync_refresh_interval) == 0
                                    if not force_refresh:
                                        state.tools_hash_record_hit(scope_key)
                                        metrics.tools_hash_sync_hits += 1
                                        metrics.tools_hash_sync_not_modified += 1
                                        await send_to_client(
                                            {
                                                "jsonrpc": msg.get("jsonrpc", "2.0"),
                                                "id": req_id,
                                                "result": {
                                                    "tools": [],
                                                    "_ultra_lean_mcp_proxy": {
                                                        "tools_hash_sync": {
                                                            "not_modified": True,
                                                            "tools_hash": value,
                                                        }
                                                    },
                                                },
                                            }
                                        )
                                        continue
                            pending[req_id] = PendingRequest(
                                method=method,
                                tools_hash_if_none_match=value,
                                tools_hash_if_none_match_provided=provided,
                                tools_hash_if_none_match_valid=valid,
                            )
                        elif method == "tools/call":
                            tool_name, arguments = _extract_tool_call(msg)

                            # Meta-tool for lazy discovery is handled fully in proxy.
                            if cfg.lazy_loading_enabled and tool_name == SEARCH_TOOL_NAME:
                                search_result = _build_search_result(state, cfg, arguments)
                                search_result = _apply_result_compression(
                                    result=search_result,
                                    tool_name=tool_name,
                                    config=cfg,
                                    metrics=metrics,
                                    token_counter=token_counter,
                                    feature_states=feature_states,
                                    key_registry=key_registry,
                                    key_registry_counter=key_registry_counter,
                                )
                                metrics.search_calls += 1
                                await send_to_client(
                                    {"jsonrpc": msg.get("jsonrpc", "2.0"), "id": req_id, "result": search_result}
                                )
                                continue

                            cache_key = None
                            if _tool_cache_allowed(cfg, tool_name):
                                cache_key = make_cache_key(cfg.session_id, cfg.server_name, tool_name, arguments)
                                cached = state.cache_get(cache_key)
                                if cached is not None:
                                    metrics.cache_hits += 1
                                    delivered = _apply_delta_response(
                                        result=cached,
                                        history_key=cache_key,
                                        tool_name=tool_name,
                                        state=state,
                                        config=cfg,
                                        metrics=metrics,
                                        delta_counters=delta_counters,
                                        token_counter=token_counter,
                                    )
                                    await send_to_client(
                                        {"jsonrpc": msg.get("jsonrpc", "2.0"), "id": req_id, "result": delivered}
                                    )
                                    continue
                                metrics.cache_misses += 1
                            pending[req_id] = PendingRequest(
                                method=method,
                                tool_name=tool_name,
                                arguments=arguments,
                                cache_key=cache_key,
                            )
                        else:
                            pending[req_id] = PendingRequest(method=method)
                    except Exception as exc:
                        logger.debug("request interception failed (fail-open): %s", exc)

                _write_jsonrpc(upstream_stdin, msg)
                metrics.upstream_requests += 1
                metrics.upstream_request_bytes += _json_bytes(msg)
                metrics.upstream_request_tokens += token_counter.count(msg)
                await upstream_stdin.drain()
        except Exception as exc:
            logger.error("client_to_upstream error: %s", exc)

    async def upstream_to_client():
        nonlocal tools_hash_sync_negotiated
        try:
            while True:
                msg = await _read_jsonrpc(upstream_stdout)
                if msg is None:
                    logger.info("Upstream EOF")
                    return
                metrics.upstream_responses += 1
                metrics.upstream_response_bytes += _json_bytes(msg)
                metrics.upstream_response_tokens += token_counter.count(msg)

                _trace_upstream(trace_rpc, msg, pending)

                req_id = msg.get("id")
                if req_id is not None and "result" in msg:
                    pending_req = pending.pop(req_id, None)
                    if pending_req and pending_req.method == "initialize":
                        if cfg.tools_hash_sync_enabled and pending_req.client_tools_hash_sync_supported:
                            tools_hash_sync_negotiated = True
                            try:
                                msg["result"] = _inject_initialize_tools_hash_capability(
                                    msg["result"],
                                    algorithm=cfg.tools_hash_sync_algorithm,
                                )
                            except Exception as exc:
                                logger.debug("initialize capability injection failed (fail-open): %s", exc)
                        else:
                            tools_hash_sync_negotiated = False
                    elif pending_req and pending_req.method == "tools/list":
                        try:
                            msg["result"] = _handle_tools_list_result(
                                msg["result"],
                                state,
                                cfg,
                                metrics,
                                token_counter,
                                tools_hash_sync_negotiated=tools_hash_sync_negotiated,
                                profile_fingerprint=profile_fingerprint,
                                if_none_match=pending_req.tools_hash_if_none_match,
                                if_none_match_provided=pending_req.tools_hash_if_none_match_provided,
                                if_none_match_valid=pending_req.tools_hash_if_none_match_valid,
                            )
                            if cfg.stats:
                                logger.info(
                                    "tools/list optimized (%d bytes saved total)",
                                    metrics.tools_list_saved_bytes,
                                )
                        except Exception as exc:
                            logger.debug("tools/list optimization failed (fail-open): %s", exc)
                    elif pending_req and pending_req.method == "tools/call":
                        try:
                            raw_upstream_result = clone_json(msg["result"])
                            result = msg["result"]
                            result = _apply_result_compression(
                                result=result,
                                tool_name=pending_req.tool_name,
                                config=cfg,
                                metrics=metrics,
                                token_counter=token_counter,
                                feature_states=feature_states,
                                key_registry=key_registry,
                                key_registry_counter=key_registry_counter,
                            )

                            if (
                                cfg.caching_enabled
                                and not cfg.cache_mutating_tools
                                and pending_req.tool_name
                                and is_mutating_tool_name(pending_req.tool_name)
                            ):
                                # Mutating/stateful calls can invalidate prior cached reads.
                                # Clear cache/history scope for this session+server to avoid stale hits.
                                scope_prefix = f"{cfg.session_id}:{cfg.server_name}:"
                                state.cache_invalidate_prefix(scope_prefix)
                                state.history_invalidate_prefix(f"cache_raw:{scope_prefix}")

                            cache_key = pending_req.cache_key
                            if cache_key and _tool_cache_allowed(cfg, pending_req.tool_name):
                                base_ttl = cfg.cache_ttl_for_tool(pending_req.tool_name)
                                ttl = base_ttl
                                if cfg.cache_adaptive_ttl and base_ttl > 0:
                                    raw_key = f"cache_raw:{cache_key}"
                                    previous_raw = state.history_get(raw_key)
                                    if previous_raw is not None:
                                        changed = previous_raw != raw_upstream_result
                                        if changed:
                                            ttl = max(cfg.cache_ttl_min_seconds, int(base_ttl * 0.5))
                                        else:
                                            ttl = min(cfg.cache_ttl_max_seconds, int(base_ttl * 1.5))
                                    ttl = min(max(ttl, cfg.cache_ttl_min_seconds), cfg.cache_ttl_max_seconds)
                                    state.history_set(raw_key, raw_upstream_result)
                                state.cache_set(cache_key, result, ttl_seconds=ttl)

                            history_key = cache_key or make_cache_key(
                                cfg.session_id,
                                cfg.server_name,
                                pending_req.tool_name or "_unknown",
                                pending_req.arguments or {},
                            )
                            result = _apply_delta_response(
                                result=result,
                                history_key=history_key,
                                tool_name=pending_req.tool_name,
                                state=state,
                                config=cfg,
                                metrics=metrics,
                                delta_counters=delta_counters,
                                token_counter=token_counter,
                            )
                            msg["result"] = result
                        except Exception as exc:
                            logger.debug("tools/call optimization failed (fail-open): %s", exc)

                elif req_id is not None and "error" in msg:
                    pending_req = pending.pop(req_id, None)
                    if pending_req and pending_req.method == "initialize":
                        tools_hash_sync_negotiated = False

                await send_to_client(msg)
        except Exception as exc:
            logger.error("upstream_to_client error: %s", exc)

    async def stderr_forwarder():
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                sys.stderr.buffer.write(line)
                sys.stderr.buffer.flush()
        except Exception:
            pass

    try:
        await asyncio.gather(client_to_upstream(), upstream_to_client(), stderr_forwarder())
    finally:
        if cfg.stats:
            logger.info(
                (
                    "Ultra Lean MCP Proxy stats | tools/list=%d saved=%dB | tools_hash_sync hit=%d miss=%d "
                    "not_modified=%d saved=%dB/%dtok | cache hit=%d miss=%d | "
                    "result_compression=%d saved=%dB | delta=%d saved=%dB | search_calls=%d | "
                    "upstream req=%d/%dtok/%dB rsp=%d/%dtok/%dB"
                ),
                metrics.tools_list_requests,
                metrics.tools_list_saved_bytes,
                metrics.tools_hash_sync_hits,
                metrics.tools_hash_sync_misses,
                metrics.tools_hash_sync_not_modified,
                metrics.tools_hash_sync_saved_bytes,
                metrics.tools_hash_sync_saved_tokens,
                metrics.cache_hits,
                metrics.cache_misses,
                metrics.result_compressions,
                metrics.result_saved_bytes,
                metrics.delta_responses,
                metrics.delta_saved_bytes,
                metrics.search_calls,
                metrics.upstream_requests,
                metrics.upstream_request_tokens,
                metrics.upstream_request_bytes,
                metrics.upstream_responses,
                metrics.upstream_response_tokens,
                metrics.upstream_response_bytes,
            )

        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()



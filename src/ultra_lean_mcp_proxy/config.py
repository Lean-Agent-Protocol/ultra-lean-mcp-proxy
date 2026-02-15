"""Runtime configuration for Ultra Lean MCP Proxy v2 proxy features."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional


def _parse_bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _deep_merge_dict(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_config_file(path: str) -> dict[str, Any]:
    data = Path(path).read_text(encoding="utf-8")
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        parsed = json.loads(data)
    elif suffix in {".yml", ".yaml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ValueError(
                "YAML config requested but PyYAML is not installed. "
                "Install `pyyaml` or use JSON config."
            ) from exc
        parsed = yaml.safe_load(data) or {}
    else:
        # Default to JSON for deterministic behavior without extra deps.
        parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise ValueError("Proxy config must be a mapping object")
    return parsed


@dataclass
class ProxyConfig:
    """Resolved proxy runtime config after file/env/CLI merge."""

    stats: bool = False
    verbose: bool = False
    session_id: str = "default"
    strict_config: bool = False

    definition_compression_enabled: bool = True
    definition_mode: str = "balanced"

    result_compression_enabled: bool = False
    result_compression_mode: str = "balanced"
    result_min_payload_bytes: int = 512
    result_strip_nulls: bool = False
    result_strip_defaults: bool = False
    result_min_token_savings_abs: int = 100
    result_min_token_savings_ratio: float = 0.05
    result_min_compressibility: float = 0.2
    result_shared_key_registry: bool = True
    result_key_bootstrap_interval: int = 8
    result_minify_redundant_text: bool = True

    delta_responses_enabled: bool = False
    delta_min_savings_ratio: float = 0.15
    delta_max_patch_bytes: int = 65536
    delta_max_patch_ratio: float = 0.8
    delta_snapshot_interval: int = 5

    lazy_loading_enabled: bool = False
    lazy_mode: str = "off"
    lazy_top_k: int = 8
    lazy_semantic: bool = False
    lazy_min_tools: int = 30
    lazy_min_tokens: int = 8000
    lazy_min_confidence_score: float = 2.0
    lazy_fallback_full_on_low_confidence: bool = True

    tools_hash_sync_enabled: bool = False
    tools_hash_sync_algorithm: str = "sha256"
    tools_hash_sync_refresh_interval: int = 50
    tools_hash_sync_include_server_fingerprint: bool = True

    caching_enabled: bool = False
    cache_ttl_seconds: int = 300
    cache_max_entries: int = 5000
    cache_errors: bool = False
    cache_mutating_tools: bool = False
    cache_adaptive_ttl: bool = True
    cache_ttl_min_seconds: int = 30
    cache_ttl_max_seconds: int = 1800

    auto_disable_enabled: bool = True
    auto_disable_threshold: int = 3
    auto_disable_cooldown_requests: int = 20

    server_name: str = "default"
    tool_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_path: Optional[str] = None

    def feature_enabled_for_tool(
        self,
        tool_name: Optional[str],
        feature_name: str,
        default: bool,
    ) -> bool:
        if not tool_name:
            return default
        tool_cfg = self.tool_overrides.get(tool_name, {})
        feature_cfg = tool_cfg.get(feature_name)
        if isinstance(feature_cfg, bool):
            return feature_cfg
        if isinstance(feature_cfg, dict):
            enabled = _parse_bool(feature_cfg.get("enabled"))
            if enabled is not None:
                return enabled
        return default

    def cache_ttl_for_tool(self, tool_name: Optional[str]) -> int:
        if not tool_name:
            return self.cache_ttl_seconds
        tool_cfg = self.tool_overrides.get(tool_name, {})
        caching_cfg = tool_cfg.get("caching")
        if isinstance(caching_cfg, dict):
            ttl = caching_cfg.get("ttl_seconds")
            if isinstance(ttl, int) and ttl >= 0:
                return ttl
        return self.cache_ttl_seconds


def _extract_server_profile(config_data: dict, upstream_command: list[str]) -> tuple[str, dict]:
    servers = config_data.get("servers", {})
    if not isinstance(servers, dict):
        return "default", {}

    command_text = " ".join(upstream_command)
    selected_name = "default"
    selected_profile = {}

    default_profile = servers.get("default", {})
    if isinstance(default_profile, dict):
        selected_profile = dict(default_profile)

    for server_name, profile in servers.items():
        if server_name == "default" or not isinstance(profile, dict):
            continue
        match = profile.get("match", {})
        if not isinstance(match, dict):
            continue
        command_contains = match.get("command_contains")
        if isinstance(command_contains, str) and command_contains in command_text:
            selected_name = server_name
            selected_profile = _deep_merge_dict(selected_profile, profile)
            break
    return selected_name, selected_profile


def _apply_global_config(
    cfg: ProxyConfig,
    config_data: dict,
    upstream_command: list[str],
    *,
    apply_server_profiles: bool = True,
) -> ProxyConfig:
    proxy = config_data.get("proxy", {})
    if isinstance(proxy, dict):
        if _parse_bool(proxy.get("stats")) is not None:
            cfg.stats = bool(_parse_bool(proxy.get("stats")))
        if _parse_bool(proxy.get("verbose")) is not None:
            cfg.verbose = bool(_parse_bool(proxy.get("verbose")))
        if isinstance(proxy.get("session_id"), str) and proxy["session_id"]:
            cfg.session_id = proxy["session_id"]
        if isinstance(proxy.get("max_sessions"), int) and proxy["max_sessions"] > 0:
            cfg.cache_max_entries = proxy["max_sessions"] * 10
        if isinstance(proxy.get("strict_config"), bool):
            cfg.strict_config = proxy["strict_config"]

    optimizations = config_data.get("optimizations", {})
    if isinstance(optimizations, dict):
        def_cfg = optimizations.get("definition_compression", {})
        if isinstance(def_cfg, dict):
            if _parse_bool(def_cfg.get("enabled")) is not None:
                cfg.definition_compression_enabled = bool(_parse_bool(def_cfg.get("enabled")))
            if isinstance(def_cfg.get("mode"), str):
                cfg.definition_mode = def_cfg["mode"]

        rcfg = optimizations.get("result_compression", {})
        if isinstance(rcfg, dict):
            if _parse_bool(rcfg.get("enabled")) is not None:
                cfg.result_compression_enabled = bool(_parse_bool(rcfg.get("enabled")))
            if isinstance(rcfg.get("mode"), str):
                cfg.result_compression_mode = rcfg["mode"]
            if isinstance(rcfg.get("min_payload_bytes"), int):
                cfg.result_min_payload_bytes = max(0, rcfg["min_payload_bytes"])
            if isinstance(rcfg.get("min_token_savings_abs"), int):
                cfg.result_min_token_savings_abs = max(0, rcfg["min_token_savings_abs"])
            if isinstance(rcfg.get("min_token_savings_ratio"), (int, float)):
                cfg.result_min_token_savings_ratio = min(max(float(rcfg["min_token_savings_ratio"]), 0.0), 1.0)
            if isinstance(rcfg.get("min_compressibility"), (int, float)):
                cfg.result_min_compressibility = min(max(float(rcfg["min_compressibility"]), 0.0), 1.0)
            if _parse_bool(rcfg.get("shared_key_registry")) is not None:
                cfg.result_shared_key_registry = bool(_parse_bool(rcfg.get("shared_key_registry")))
            if isinstance(rcfg.get("key_bootstrap_interval"), int):
                cfg.result_key_bootstrap_interval = max(0, rcfg["key_bootstrap_interval"])
            if _parse_bool(rcfg.get("minify_redundant_text")) is not None:
                cfg.result_minify_redundant_text = bool(_parse_bool(rcfg.get("minify_redundant_text")))
            if _parse_bool(rcfg.get("strip_nulls")) is not None:
                cfg.result_strip_nulls = bool(_parse_bool(rcfg.get("strip_nulls")))
            if _parse_bool(rcfg.get("strip_defaults")) is not None:
                cfg.result_strip_defaults = bool(_parse_bool(rcfg.get("strip_defaults")))

        dcfg = optimizations.get("delta_responses", {})
        if isinstance(dcfg, dict):
            if _parse_bool(dcfg.get("enabled")) is not None:
                cfg.delta_responses_enabled = bool(_parse_bool(dcfg.get("enabled")))
            if isinstance(dcfg.get("min_savings_ratio"), (int, float)):
                ratio = float(dcfg["min_savings_ratio"])
                cfg.delta_min_savings_ratio = min(max(ratio, 0.0), 1.0)
            if isinstance(dcfg.get("max_patch_bytes"), int):
                cfg.delta_max_patch_bytes = max(0, dcfg["max_patch_bytes"])
            if isinstance(dcfg.get("max_patch_ratio"), (int, float)):
                ratio = float(dcfg["max_patch_ratio"])
                cfg.delta_max_patch_ratio = min(max(ratio, 0.0), 1.0)
            if isinstance(dcfg.get("snapshot_interval"), int):
                cfg.delta_snapshot_interval = max(1, dcfg["snapshot_interval"])

        lcfg = optimizations.get("lazy_loading", {})
        if isinstance(lcfg, dict):
            if _parse_bool(lcfg.get("enabled")) is not None:
                cfg.lazy_loading_enabled = bool(_parse_bool(lcfg.get("enabled")))
            if isinstance(lcfg.get("mode"), str):
                cfg.lazy_mode = lcfg["mode"]
            if isinstance(lcfg.get("top_k"), int):
                cfg.lazy_top_k = max(1, lcfg["top_k"])
            if isinstance(lcfg.get("min_tools"), int):
                cfg.lazy_min_tools = max(0, lcfg["min_tools"])
            if isinstance(lcfg.get("min_tokens"), int):
                cfg.lazy_min_tokens = max(0, lcfg["min_tokens"])
            if isinstance(lcfg.get("min_confidence_score"), (int, float)):
                cfg.lazy_min_confidence_score = float(lcfg["min_confidence_score"])
            if _parse_bool(lcfg.get("fallback_full_on_low_confidence")) is not None:
                cfg.lazy_fallback_full_on_low_confidence = bool(
                    _parse_bool(lcfg.get("fallback_full_on_low_confidence"))
                )
            if _parse_bool(lcfg.get("semantic")) is not None:
                cfg.lazy_semantic = bool(_parse_bool(lcfg.get("semantic")))

        hcfg = optimizations.get("tools_hash_sync", {})
        if isinstance(hcfg, dict):
            if _parse_bool(hcfg.get("enabled")) is not None:
                cfg.tools_hash_sync_enabled = bool(_parse_bool(hcfg.get("enabled")))
            if isinstance(hcfg.get("algorithm"), str):
                cfg.tools_hash_sync_algorithm = hcfg["algorithm"].strip().lower()
            if isinstance(hcfg.get("refresh_interval"), int):
                cfg.tools_hash_sync_refresh_interval = max(1, hcfg["refresh_interval"])
            if _parse_bool(hcfg.get("include_server_fingerprint")) is not None:
                cfg.tools_hash_sync_include_server_fingerprint = bool(
                    _parse_bool(hcfg.get("include_server_fingerprint"))
                )

        ccfg = optimizations.get("caching", {})
        if isinstance(ccfg, dict):
            if _parse_bool(ccfg.get("enabled")) is not None:
                cfg.caching_enabled = bool(_parse_bool(ccfg.get("enabled")))
            if isinstance(ccfg.get("default_ttl_seconds"), int):
                cfg.cache_ttl_seconds = max(0, ccfg["default_ttl_seconds"])
            if isinstance(ccfg.get("max_entries"), int):
                cfg.cache_max_entries = max(1, ccfg["max_entries"])
            if _parse_bool(ccfg.get("cache_errors")) is not None:
                cfg.cache_errors = bool(_parse_bool(ccfg.get("cache_errors")))
            if _parse_bool(ccfg.get("cache_mutating_tools")) is not None:
                cfg.cache_mutating_tools = bool(_parse_bool(ccfg.get("cache_mutating_tools")))
            if _parse_bool(ccfg.get("adaptive_ttl")) is not None:
                cfg.cache_adaptive_ttl = bool(_parse_bool(ccfg.get("adaptive_ttl")))
            if isinstance(ccfg.get("ttl_min_seconds"), int):
                cfg.cache_ttl_min_seconds = max(0, ccfg["ttl_min_seconds"])
            if isinstance(ccfg.get("ttl_max_seconds"), int):
                cfg.cache_ttl_max_seconds = max(0, ccfg["ttl_max_seconds"])

        acfg = optimizations.get("auto_disable", {})
        if isinstance(acfg, dict):
            if _parse_bool(acfg.get("enabled")) is not None:
                cfg.auto_disable_enabled = bool(_parse_bool(acfg.get("enabled")))
            if isinstance(acfg.get("threshold"), int):
                cfg.auto_disable_threshold = max(1, acfg["threshold"])
            if isinstance(acfg.get("cooldown_requests"), int):
                cfg.auto_disable_cooldown_requests = max(1, acfg["cooldown_requests"])

    if apply_server_profiles:
        server_name, profile = _extract_server_profile(config_data, upstream_command)
        cfg.server_name = server_name
        if isinstance(profile, dict) and profile:
            profile_opts = {}
            if isinstance(profile.get("proxy"), dict):
                profile_opts["proxy"] = profile["proxy"]
            if isinstance(profile.get("optimizations"), dict):
                profile_opts["optimizations"] = profile["optimizations"]
            if profile_opts:
                cfg = _apply_global_config(
                    cfg,
                    profile_opts,
                    upstream_command,
                    apply_server_profiles=False,
                )
            tools = profile.get("tools", {})
            if isinstance(tools, dict):
                cfg.tool_overrides = _deep_merge_dict(cfg.tool_overrides, tools)

    return cfg


def _apply_env(cfg: ProxyConfig, env: Mapping[str, str]) -> ProxyConfig:
    if _parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_STATS")) is not None:
        cfg.stats = bool(_parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_STATS")))
    if _parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_VERBOSE")) is not None:
        cfg.verbose = bool(_parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_VERBOSE")))
    if env.get("ULTRA_LEAN_MCP_PROXY_SESSION_ID"):
        cfg.session_id = env["ULTRA_LEAN_MCP_PROXY_SESSION_ID"]

    if _parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_RESULT_COMPRESSION")) is not None:
        cfg.result_compression_enabled = bool(_parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_RESULT_COMPRESSION")))
    if env.get("ULTRA_LEAN_MCP_PROXY_RESULT_COMPRESSION_MODE"):
        cfg.result_compression_mode = env["ULTRA_LEAN_MCP_PROXY_RESULT_COMPRESSION_MODE"]
    if env.get("ULTRA_LEAN_MCP_PROXY_RESULT_MIN_TOKEN_SAVINGS_ABS"):
        try:
            cfg.result_min_token_savings_abs = max(0, int(env["ULTRA_LEAN_MCP_PROXY_RESULT_MIN_TOKEN_SAVINGS_ABS"]))
        except ValueError:
            pass
    if env.get("ULTRA_LEAN_MCP_PROXY_RESULT_MIN_TOKEN_SAVINGS_RATIO"):
        try:
            ratio = float(env["ULTRA_LEAN_MCP_PROXY_RESULT_MIN_TOKEN_SAVINGS_RATIO"])
            cfg.result_min_token_savings_ratio = min(max(ratio, 0.0), 1.0)
        except ValueError:
            pass
    if _parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_RESULT_SHARED_KEY_REGISTRY")) is not None:
        cfg.result_shared_key_registry = bool(_parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_RESULT_SHARED_KEY_REGISTRY")))
    if env.get("ULTRA_LEAN_MCP_PROXY_RESULT_KEY_BOOTSTRAP_INTERVAL"):
        try:
            cfg.result_key_bootstrap_interval = max(0, int(env["ULTRA_LEAN_MCP_PROXY_RESULT_KEY_BOOTSTRAP_INTERVAL"]))
        except ValueError:
            pass
    if _parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_RESULT_MINIFY_REDUNDANT_TEXT")) is not None:
        cfg.result_minify_redundant_text = bool(_parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_RESULT_MINIFY_REDUNDANT_TEXT")))

    if _parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_DELTA_RESPONSES")) is not None:
        cfg.delta_responses_enabled = bool(_parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_DELTA_RESPONSES")))
    if env.get("ULTRA_LEAN_MCP_PROXY_DELTA_MIN_SAVINGS"):
        try:
            cfg.delta_min_savings_ratio = min(max(float(env["ULTRA_LEAN_MCP_PROXY_DELTA_MIN_SAVINGS"]), 0.0), 1.0)
        except ValueError:
            pass
    if env.get("ULTRA_LEAN_MCP_PROXY_DELTA_MAX_PATCH_RATIO"):
        try:
            ratio = float(env["ULTRA_LEAN_MCP_PROXY_DELTA_MAX_PATCH_RATIO"])
            cfg.delta_max_patch_ratio = min(max(ratio, 0.0), 1.0)
        except ValueError:
            pass

    if _parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_LAZY_LOADING")) is not None:
        cfg.lazy_loading_enabled = bool(_parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_LAZY_LOADING")))
    if env.get("ULTRA_LEAN_MCP_PROXY_LAZY_MODE"):
        cfg.lazy_mode = env["ULTRA_LEAN_MCP_PROXY_LAZY_MODE"]
    if env.get("ULTRA_LEAN_MCP_PROXY_SEARCH_TOP_K"):
        try:
            cfg.lazy_top_k = max(1, int(env["ULTRA_LEAN_MCP_PROXY_SEARCH_TOP_K"]))
        except ValueError:
            pass
    if env.get("ULTRA_LEAN_MCP_PROXY_LAZY_MIN_TOOLS"):
        try:
            cfg.lazy_min_tools = max(0, int(env["ULTRA_LEAN_MCP_PROXY_LAZY_MIN_TOOLS"]))
        except ValueError:
            pass
    if env.get("ULTRA_LEAN_MCP_PROXY_LAZY_MIN_TOKENS"):
        try:
            cfg.lazy_min_tokens = max(0, int(env["ULTRA_LEAN_MCP_PROXY_LAZY_MIN_TOKENS"]))
        except ValueError:
            pass
    if env.get("ULTRA_LEAN_MCP_PROXY_LAZY_MIN_CONFIDENCE"):
        try:
            cfg.lazy_min_confidence_score = float(env["ULTRA_LEAN_MCP_PROXY_LAZY_MIN_CONFIDENCE"])
        except ValueError:
            pass

    if _parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_TOOLS_HASH_SYNC")) is not None:
        cfg.tools_hash_sync_enabled = bool(_parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_TOOLS_HASH_SYNC")))
    if env.get("ULTRA_LEAN_MCP_PROXY_TOOLS_HASH_REFRESH_INTERVAL"):
        try:
            cfg.tools_hash_sync_refresh_interval = max(1, int(env["ULTRA_LEAN_MCP_PROXY_TOOLS_HASH_REFRESH_INTERVAL"]))
        except ValueError:
            pass

    if _parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_CACHING")) is not None:
        cfg.caching_enabled = bool(_parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_CACHING")))
    if env.get("ULTRA_LEAN_MCP_PROXY_CACHE_TTL_SECONDS"):
        try:
            cfg.cache_ttl_seconds = max(0, int(env["ULTRA_LEAN_MCP_PROXY_CACHE_TTL_SECONDS"]))
        except ValueError:
            pass
    if _parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_CACHE_ADAPTIVE_TTL")) is not None:
        cfg.cache_adaptive_ttl = bool(_parse_bool(env.get("ULTRA_LEAN_MCP_PROXY_CACHE_ADAPTIVE_TTL")))
    return cfg


def _apply_cli_overrides(cfg: ProxyConfig, cli: Mapping[str, Any]) -> ProxyConfig:
    def _set_bool(name: str, target_attr: str):
        value = cli.get(name)
        if value is not None:
            setattr(cfg, target_attr, bool(value))

    _set_bool("stats", "stats")
    _set_bool("verbose", "verbose")
    _set_bool("result_compression", "result_compression_enabled")
    _set_bool("delta_responses", "delta_responses_enabled")
    _set_bool("lazy_loading", "lazy_loading_enabled")
    _set_bool("tools_hash_sync", "tools_hash_sync_enabled")
    _set_bool("caching", "caching_enabled")

    if cli.get("session_id"):
        cfg.session_id = str(cli["session_id"])
    if cli.get("strict_config") is not None:
        cfg.strict_config = bool(cli["strict_config"])

    if cli.get("cache_ttl") is not None:
        cfg.cache_ttl_seconds = max(0, int(cli["cache_ttl"]))
    if cli.get("delta_min_savings") is not None:
        cfg.delta_min_savings_ratio = min(max(float(cli["delta_min_savings"]), 0.0), 1.0)
    if cli.get("lazy_mode"):
        cfg.lazy_mode = str(cli["lazy_mode"])
    if cli.get("search_top_k") is not None:
        cfg.lazy_top_k = max(1, int(cli["search_top_k"]))
    if cli.get("result_compression_mode"):
        cfg.result_compression_mode = str(cli["result_compression_mode"])
    if cli.get("tools_hash_refresh_interval") is not None:
        cfg.tools_hash_sync_refresh_interval = max(1, int(cli["tools_hash_refresh_interval"]))
    return cfg


def load_proxy_config(
    upstream_command: list[str],
    config_path: Optional[str] = None,
    cli_overrides: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> ProxyConfig:
    """Resolve proxy config from defaults + file + env + CLI."""
    env_map = env or os.environ
    cli = dict(cli_overrides or {})
    cfg = ProxyConfig()

    resolved_path = config_path or cli.get("config_path") or env_map.get("ULTRA_LEAN_MCP_PROXY_CONFIG")
    if resolved_path:
        config_data = _read_config_file(resolved_path)
        cfg = _apply_global_config(cfg, config_data, upstream_command)
        cfg.source_path = resolved_path

    cfg = _apply_env(cfg, env_map)
    cfg = _apply_cli_overrides(cfg, cli)

    if cfg.lazy_mode not in {"off", "minimal", "search_only"}:
        raise ValueError(f"Invalid lazy mode: {cfg.lazy_mode}")
    if cfg.result_compression_mode not in {"off", "balanced", "aggressive"}:
        raise ValueError(f"Invalid result compression mode: {cfg.result_compression_mode}")
    if cfg.tools_hash_sync_algorithm != "sha256":
        raise ValueError(f"Invalid tools hash sync algorithm: {cfg.tools_hash_sync_algorithm}")
    if cfg.cache_ttl_max_seconds < cfg.cache_ttl_min_seconds:
        cfg.cache_ttl_max_seconds = cfg.cache_ttl_min_seconds

    # Convenience: lazy mode implies lazy loading enabled.
    if cfg.lazy_mode != "off":
        cfg.lazy_loading_enabled = True

    # Off mode disables vector regardless of bool flags.
    if cfg.result_compression_mode == "off":
        cfg.result_compression_enabled = False

    return cfg



"""Tests for v2 proxy config resolution."""

import json

from ultra_lean_mcp_proxy.config import load_proxy_config


def test_load_config_with_server_and_tool_overrides(tmp_path):
    config_path = tmp_path / "ultra-lean-mcp-proxy.config.json"
    config_path.write_text(
        json.dumps(
            {
                "optimizations": {
                    "result_compression": {"enabled": False},
                    "caching": {"enabled": False, "default_ttl_seconds": 300},
                    "tools_hash_sync": {"enabled": True, "refresh_interval": 9},
                },
                "servers": {
                    "default": {
                        "tools": {
                            "list_items": {"caching": {"enabled": True, "ttl_seconds": 10}},
                        }
                    },
                    "github": {
                        "match": {"command_contains": "server-github"},
                        "optimizations": {
                            "caching": {"enabled": True, "default_ttl_seconds": 30},
                            "lazy_loading": {"enabled": True, "mode": "minimal"},
                            "tools_hash_sync": {"enabled": True, "refresh_interval": 3},
                        },
                        "tools": {
                            "create_issue": {"caching": {"enabled": False}},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_proxy_config(
        upstream_command=["npx", "@modelcontextprotocol/server-github"],
        config_path=str(config_path),
    )
    assert cfg.server_name == "github"
    assert cfg.caching_enabled is True
    assert cfg.cache_ttl_seconds == 30
    assert cfg.lazy_loading_enabled is True
    assert cfg.lazy_mode == "minimal"
    assert cfg.tools_hash_sync_enabled is True
    assert cfg.tools_hash_sync_refresh_interval == 3
    assert "create_issue" in cfg.tool_overrides
    assert "list_items" in cfg.tool_overrides


def test_cli_overrides_take_precedence(tmp_path):
    config_path = tmp_path / "ultra-lean-mcp-proxy.config.json"
    config_path.write_text(
        json.dumps(
            {
                "optimizations": {
                    "caching": {"enabled": False, "default_ttl_seconds": 15},
                    "delta_responses": {"enabled": False},
                    "tools_hash_sync": {"enabled": False, "refresh_interval": 10},
                }
            }
        ),
        encoding="utf-8",
    )

    cfg = load_proxy_config(
        upstream_command=["python", "fake_server.py"],
        config_path=str(config_path),
        cli_overrides={
            "caching": True,
            "cache_ttl": 120,
            "delta_responses": True,
            "tools_hash_sync": True,
            "tools_hash_refresh_interval": 4,
        },
    )
    assert cfg.caching_enabled is True
    assert cfg.cache_ttl_seconds == 120
    assert cfg.delta_responses_enabled is True
    assert cfg.tools_hash_sync_enabled is True
    assert cfg.tools_hash_sync_refresh_interval == 4


def test_heuristic_knobs_are_loaded(tmp_path):
    config_path = tmp_path / "ultra-lean-mcp-proxy.config.json"
    config_path.write_text(
        json.dumps(
            {
                "optimizations": {
                    "result_compression": {
                        "enabled": True,
                        "min_token_savings_abs": 120,
                        "min_token_savings_ratio": 0.08,
                        "min_compressibility": 0.25,
                    },
                    "delta_responses": {
                        "enabled": True,
                        "max_patch_ratio": 0.7,
                        "snapshot_interval": 4,
                    },
                    "lazy_loading": {
                        "enabled": True,
                        "mode": "minimal",
                        "min_tools": 40,
                        "min_tokens": 9000,
                        "min_confidence_score": 2.5,
                        "fallback_full_on_low_confidence": True,
                    },
                    "caching": {
                        "enabled": True,
                        "adaptive_ttl": True,
                        "ttl_min_seconds": 15,
                        "ttl_max_seconds": 900,
                    },
                    "tools_hash_sync": {
                        "enabled": True,
                        "algorithm": "sha256",
                        "refresh_interval": 6,
                        "include_server_fingerprint": False,
                    },
                    "auto_disable": {
                        "enabled": True,
                        "threshold": 4,
                        "cooldown_requests": 25,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_proxy_config(
        upstream_command=["python", "fake_server.py"],
        config_path=str(config_path),
    )
    assert cfg.result_min_token_savings_abs == 120
    assert cfg.result_min_token_savings_ratio == 0.08
    assert cfg.result_min_compressibility == 0.25
    assert cfg.delta_max_patch_ratio == 0.7
    assert cfg.delta_snapshot_interval == 4
    assert cfg.lazy_min_tools == 40
    assert cfg.lazy_min_tokens == 9000
    assert cfg.lazy_min_confidence_score == 2.5
    assert cfg.cache_adaptive_ttl is True
    assert cfg.cache_ttl_min_seconds == 15
    assert cfg.cache_ttl_max_seconds == 900
    assert cfg.tools_hash_sync_enabled is True
    assert cfg.tools_hash_sync_refresh_interval == 6
    assert cfg.tools_hash_sync_include_server_fingerprint is False
    assert cfg.auto_disable_enabled is True
    assert cfg.auto_disable_threshold == 4
    assert cfg.auto_disable_cooldown_requests == 25


def test_tools_hash_sync_env_override_and_validation(tmp_path):
    config_path = tmp_path / "ultra-lean-mcp-proxy.config.json"
    config_path.write_text(
        json.dumps(
            {
                "optimizations": {
                    "tools_hash_sync": {
                        "enabled": False,
                        "algorithm": "sha256",
                        "refresh_interval": 50,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    cfg = load_proxy_config(
        upstream_command=["python", "fake_server.py"],
        config_path=str(config_path),
        env={
            "ULTRA_LEAN_MCP_PROXY_TOOLS_HASH_SYNC": "1",
            "ULTRA_LEAN_MCP_PROXY_TOOLS_HASH_REFRESH_INTERVAL": "2",
        },
    )
    assert cfg.tools_hash_sync_enabled is True
    assert cfg.tools_hash_sync_refresh_interval == 2


def test_tools_hash_sync_invalid_algorithm_raises(tmp_path):
    config_path = tmp_path / "ultra-lean-mcp-proxy.config.json"
    config_path.write_text(
        json.dumps(
            {
                "optimizations": {
                    "tools_hash_sync": {
                        "enabled": True,
                        "algorithm": "sha1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    try:
        load_proxy_config(
            upstream_command=["python", "fake_server.py"],
            config_path=str(config_path),
        )
        assert False, "expected ValueError for unsupported tools hash algorithm"
    except ValueError as exc:
        assert "tools hash sync algorithm" in str(exc).lower()


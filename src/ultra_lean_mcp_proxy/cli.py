"""CLI entry point for Ultra Lean MCP Proxy."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .config import load_proxy_config


def _add_bool_toggle(parser: argparse.ArgumentParser, name: str, help_text: str):
    parser.add_argument(
        f"--enable-{name}",
        dest=name.replace("-", "_"),
        action="store_true",
        help=f"Enable {help_text}",
    )
    parser.add_argument(
        f"--disable-{name}",
        dest=name.replace("-", "_"),
        action="store_false",
        help=f"Disable {help_text}",
    )


def main():
    parser = argparse.ArgumentParser(
        prog="ultra-lean-mcp-proxy",
        description="Ultra Lean MCP Proxy - optimize MCP traffic",
    )
    sub = parser.add_subparsers(dest="command")

    p_proxy = sub.add_parser("proxy", help="Run as MCP proxy with optional optimization vectors")
    p_proxy.add_argument("--stats", action="store_true", help="Log optimization statistics to stderr")
    p_proxy.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    p_proxy.add_argument("--config", help="Path to Ultra Lean MCP Proxy config (JSON or YAML)")
    p_proxy.add_argument("--session-id", help="Session id for cache/delta state")
    p_proxy.add_argument("--strict-config", action="store_true", default=None, help="Strict config validation")

    _add_bool_toggle(p_proxy, "result-compression", "result compression")
    _add_bool_toggle(p_proxy, "delta-responses", "delta responses")
    _add_bool_toggle(p_proxy, "lazy-loading", "lazy loading")
    _add_bool_toggle(p_proxy, "tools-hash-sync", "tools hash sync")
    _add_bool_toggle(p_proxy, "caching", "caching")

    p_proxy.add_argument("--cache-ttl", type=int, help="Default cache TTL in seconds")
    p_proxy.add_argument("--delta-min-savings", type=float, help="Minimum savings ratio for delta emission")
    p_proxy.add_argument("--lazy-mode", choices=["off", "minimal", "search_only"], help="Lazy loading mode")
    p_proxy.add_argument("--tools-hash-refresh-interval", type=int, help="Force full snapshot every N conditional hits")
    p_proxy.add_argument("--search-top-k", type=int, help="Default top-k for search tool")
    p_proxy.add_argument(
        "--result-compression-mode",
        choices=["off", "balanced", "aggressive"],
        help="Result compression mode",
    )
    p_proxy.add_argument("--dump-effective-config", action="store_true", help="Print resolved config to stderr")

    p_proxy.set_defaults(
        result_compression=None,
        delta_responses=None,
        lazy_loading=None,
        tools_hash_sync=None,
        caching=None,
    )

    p_proxy.add_argument("upstream", nargs=argparse.REMAINDER, help="Upstream MCP server command (after --)")

    args = parser.parse_args()

    if args.command == "proxy":
        upstream = args.upstream
        if upstream and upstream[0] == "--":
            upstream = upstream[1:]
        if not upstream:
            print("Error: No upstream server command provided.", file=sys.stderr)
            print("Usage: ultra-lean-mcp-proxy proxy -- <command> [args...]", file=sys.stderr)
            print(
                "Example: ultra-lean-mcp-proxy proxy -- npx @modelcontextprotocol/server-filesystem /tmp",
                file=sys.stderr,
            )
            sys.exit(1)

        cli_overrides = {
            "stats": args.stats,
            "verbose": args.verbose,
            "session_id": args.session_id,
            "strict_config": args.strict_config,
            "result_compression": args.result_compression,
            "delta_responses": args.delta_responses,
            "lazy_loading": args.lazy_loading,
            "tools_hash_sync": args.tools_hash_sync,
            "caching": args.caching,
            "cache_ttl": args.cache_ttl,
            "delta_min_savings": args.delta_min_savings,
            "lazy_mode": args.lazy_mode,
            "tools_hash_refresh_interval": args.tools_hash_refresh_interval,
            "search_top_k": args.search_top_k,
            "result_compression_mode": args.result_compression_mode,
            "config_path": args.config,
        }

        config = load_proxy_config(
            upstream_command=upstream,
            config_path=args.config,
            cli_overrides=cli_overrides,
        )

        level = logging.DEBUG if config.verbose else logging.INFO
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            stream=sys.stderr,
        )

        if args.dump_effective_config:
            print(
                json.dumps(
                    {
                        "stats": config.stats,
                        "verbose": config.verbose,
                        "session_id": config.session_id,
                        "server_name": config.server_name,
                        "definition_compression_enabled": config.definition_compression_enabled,
                        "result_compression_enabled": config.result_compression_enabled,
                        "result_compression_mode": config.result_compression_mode,
                        "result_min_token_savings_abs": config.result_min_token_savings_abs,
                        "result_min_token_savings_ratio": config.result_min_token_savings_ratio,
                        "result_min_compressibility": config.result_min_compressibility,
                        "result_shared_key_registry": config.result_shared_key_registry,
                        "result_key_bootstrap_interval": config.result_key_bootstrap_interval,
                        "result_minify_redundant_text": config.result_minify_redundant_text,
                        "delta_responses_enabled": config.delta_responses_enabled,
                        "delta_max_patch_ratio": config.delta_max_patch_ratio,
                        "delta_snapshot_interval": config.delta_snapshot_interval,
                        "lazy_loading_enabled": config.lazy_loading_enabled,
                        "lazy_mode": config.lazy_mode,
                        "lazy_min_tools": config.lazy_min_tools,
                        "lazy_min_tokens": config.lazy_min_tokens,
                        "lazy_min_confidence_score": config.lazy_min_confidence_score,
                        "tools_hash_sync_enabled": config.tools_hash_sync_enabled,
                        "tools_hash_sync_algorithm": config.tools_hash_sync_algorithm,
                        "tools_hash_sync_refresh_interval": config.tools_hash_sync_refresh_interval,
                        "tools_hash_sync_include_server_fingerprint": config.tools_hash_sync_include_server_fingerprint,
                        "caching_enabled": config.caching_enabled,
                        "cache_ttl_seconds": config.cache_ttl_seconds,
                        "cache_adaptive_ttl": config.cache_adaptive_ttl,
                        "cache_ttl_min_seconds": config.cache_ttl_min_seconds,
                        "cache_ttl_max_seconds": config.cache_ttl_max_seconds,
                        "auto_disable_enabled": config.auto_disable_enabled,
                        "auto_disable_threshold": config.auto_disable_threshold,
                        "auto_disable_cooldown_requests": config.auto_disable_cooldown_requests,
                        "tool_overrides": config.tool_overrides,
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )

        from .proxy import run_proxy

        asyncio.run(run_proxy(upstream, config=config, stats=config.stats))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

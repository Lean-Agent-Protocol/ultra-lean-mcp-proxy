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
    p_proxy.add_argument("--runtime", help=argparse.SUPPRESS)

    p_proxy.set_defaults(
        result_compression=None,
        delta_responses=None,
        lazy_loading=None,
        tools_hash_sync=None,
        caching=None,
    )

    p_proxy.add_argument("upstream", nargs=argparse.REMAINDER, help="Upstream MCP server command (after --)")

    # -- install subcommand --
    p_install = sub.add_parser("install", help="Wrap all MCP server configs to use the proxy")
    p_install.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    p_install.add_argument("--client", dest="client_filter", metavar="NAME", help="Only process this client config")
    p_install.add_argument(
        "--skip",
        dest="skip_names",
        metavar="NAME",
        action="append",
        help="Skip this MCP server name (repeatable)",
    )
    p_install.add_argument("--offline", action="store_true", help="Skip remote registry fetch")
    p_install.add_argument(
        "--include-url",
        dest="wrap_url",
        action="store_true",
        help="Wrap URL/SSE/HTTP entries too (default)",
    )
    p_install.add_argument(
        "--no-wrap-url",
        dest="wrap_url",
        action="store_false",
        help="Do not wrap URL/SSE/HTTP entries",
    )
    p_install.set_defaults(wrap_url=True)
    p_install.add_argument("--no-cloud", dest="no_cloud", action="store_true", help="Skip cloud connector discovery")
    p_install.add_argument("--suffix", default="-ulmp", help="Suffix for cloud-mirrored server names (default: -ulmp)")
    p_install.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    p_install.add_argument("--runtime", default="pip", choices=["pip", "npm"], help="Runtime marker (default: pip)")

    # -- uninstall subcommand --
    p_uninstall = sub.add_parser("uninstall", help="Remove proxy wrapping from all MCP server configs")
    p_uninstall.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    p_uninstall.add_argument("--client", dest="client_filter", metavar="NAME", help="Only process this client config")
    p_uninstall.add_argument("--all", dest="all_runtimes", action="store_true", help="Unwrap all runtimes")
    p_uninstall.add_argument("--runtime", default="pip", choices=["pip", "npm"], help="Runtime marker (default: pip)")
    p_uninstall.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")

    # -- status subcommand --
    sub.add_parser("status", help="Show proxy wrapping status for all MCP server configs")

    # -- watch subcommand --
    p_watch = sub.add_parser("watch", help="Watch config files and auto-wrap new MCP servers")
    p_watch.add_argument("--interval", type=float, default=5.0, help="Poll interval in seconds (default: 5)")
    p_watch.add_argument("--daemon", action="store_true", help="Run as background daemon")
    p_watch.add_argument("--stop", action="store_true", help="Stop running daemon")
    p_watch.add_argument("--offline", action="store_true", help="Skip remote registry fetch")
    p_watch.add_argument(
        "--include-url",
        dest="wrap_url",
        action="store_true",
        help="Wrap URL/SSE/HTTP entries too (default)",
    )
    p_watch.add_argument(
        "--no-wrap-url",
        dest="wrap_url",
        action="store_false",
        help="Do not wrap URL/SSE/HTTP entries",
    )
    p_watch.set_defaults(wrap_url=True)
    p_watch.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    p_watch.add_argument("--runtime", default="pip", choices=["pip", "npm"], help="Runtime marker (default: pip)")
    p_watch.add_argument("--suffix", default="-ulmp", help="Suffix for cloud-mirrored server names")
    p_watch.add_argument(
        "--cloud-interval",
        type=float,
        default=60.0,
        help="Cloud discovery poll interval in seconds (default: 60)",
    )

    # -- wrap-cloud subcommand --
    p_wrap_cloud = sub.add_parser("wrap-cloud", help="Mirror cloud-scoped Claude URL connectors, already wrapped")
    p_wrap_cloud.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    p_wrap_cloud.add_argument("--runtime", default="pip", choices=["pip", "npm"], help="Runtime marker (default: pip)")
    p_wrap_cloud.add_argument("--suffix", default="-ulmp", help="Suffix for mirror server names")
    p_wrap_cloud.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")

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

    elif args.command == "install":
        _run_install(args)

    elif args.command == "uninstall":
        _run_uninstall(args)

    elif args.command == "status":
        _run_status()

    elif args.command == "wrap-cloud":
        _run_wrap_cloud(args)

    elif args.command == "watch":
        from .watcher import watch_configs, start_daemon, stop_daemon

        if args.stop:
            stop_daemon()
        elif args.daemon:
            start_daemon(
                interval=args.interval,
                runtime=args.runtime,
                offline=args.offline,
                wrap_url=args.wrap_url,
                verbose=args.verbose,
                suffix=args.suffix,
                cloud_interval=args.cloud_interval,
            )
        else:
            level = logging.DEBUG if args.verbose else logging.INFO
            logging.basicConfig(
                level=level,
                format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                stream=sys.stderr,
            )
            watch_configs(
                interval=args.interval,
                runtime=args.runtime,
                offline=args.offline,
                wrap_url=args.wrap_url,
                verbose=args.verbose,
                suffix=args.suffix,
                cloud_interval=args.cloud_interval,
            )

    else:
        parser.print_help()
        sys.exit(1)


# ---------------------------------------------------------------------------
# Install / Uninstall / Status CLI handlers
# ---------------------------------------------------------------------------


def _run_install(args: argparse.Namespace) -> None:
    from .installer import install

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(message)s", stream=sys.stderr)

    try:
        result = install(
            dry_run=args.dry_run,
            client_filter=args.client_filter,
            skip_names=args.skip_names,
            offline=args.offline,
            wrap_url=args.wrap_url,
            verbose=args.verbose,
            runtime=args.runtime,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("(dry-run) No files were modified.\n")

    for cfg in result["configs"]:
        if cfg.get("error"):
            print(f"{cfg['name']}: {cfg['error']}")
            continue

        print(f"{cfg['name']}: {cfg['path']}")
        if cfg.get("backup") and cfg["backup"] != "(dry-run)":
            print(f"  Backup: {cfg['backup']}")

        for srv in cfg.get("servers", []):
            if srv["action"] == "wrapped":
                origin = srv.get("origin", "stdio")
                print(f"  [+] {srv['name']}: wrapped ({srv['runtime']}, origin={origin})")
            elif srv["action"] == "skipped":
                print(f"  [~] {srv['name']}: skipped ({srv['reason']})")

    print()
    print(f"Total: {result['total_found']} servers found, {result['total_wrapped']} wrapped.")

    # Cloud connector discovery (enabled by default, opt-out with --no-cloud)
    if not args.no_cloud:
        import shutil

        if shutil.which("claude"):
            from .installer import wrap_cloud

            try:
                wrap_cloud(
                    dry_run=args.dry_run,
                    runtime=args.runtime,
                    suffix=args.suffix,
                    verbose=args.verbose,
                )
            except Exception as exc:
                print(f"[install] Cloud connector discovery failed: {exc}", file=sys.stderr)
        else:
            print("[install] Cloud connector discovery skipped: claude CLI not found on PATH", file=sys.stderr)


def _run_uninstall(args: argparse.Namespace) -> None:
    from .installer import uninstall

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(message)s", stream=sys.stderr)

    result = uninstall(
        dry_run=args.dry_run,
        client_filter=args.client_filter,
        all_runtimes=args.all_runtimes,
        runtime=args.runtime,
        verbose=args.verbose,
    )

    if args.dry_run:
        print("(dry-run) No files were modified.\n")

    for cfg in result["configs"]:
        if cfg.get("error"):
            print(f"{cfg['name']}: {cfg['error']}")
            continue

        print(f"{cfg['name']}: {cfg['path']}")
        if cfg.get("backup") and cfg["backup"] != "(dry-run)":
            print(f"  Backup: {cfg['backup']}")

        for srv in cfg.get("servers", []):
            if srv["action"] == "unwrapped":
                print(f"  [-] {srv['name']}: unwrapped (was {srv['runtime']})")
            elif srv["action"] == "skipped":
                print(f"  [~] {srv['name']}: skipped ({srv['reason']})")

    print()
    print(f"Total: {result['total_found']} servers found, {result['total_unwrapped']} unwrapped.")


def _run_wrap_cloud(args: argparse.Namespace) -> None:
    from .installer import wrap_cloud

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(message)s", stream=sys.stderr)

    try:
        result = wrap_cloud(
            dry_run=args.dry_run,
            runtime=args.runtime,
            suffix=args.suffix,
            verbose=args.verbose,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _run_status() -> None:
    from .installer import status_summary

    result = status_summary()

    for cfg in result["configs"]:
        if not cfg["exists"]:
            print(f"{cfg['name']}: not found ({cfg['path']})")
            continue

        if cfg.get("error"):
            print(f"{cfg['name']}: {cfg['error']}")
            continue

        print(f"{cfg['name']}: {cfg['path']}")
        for srv in cfg.get("servers", []):
            if srv["status"] == "wrapped":
                origin = srv.get("origin", "stdio")
                print(f"  [*] {srv['name']}: wrapped ({srv['runtime']}, origin={origin})")
            elif srv["status"] == "unwrapped":
                print(f"  [ ] {srv['name']}: not wrapped")
            elif srv["status"] == "remote-unwrapped":
                print(f"  [~] {srv['name']}: remote (unwrapped)")
            elif srv["status"] == "non-stdio":
                print(f"  [~] {srv['name']}: non-stdio (skipped)")

    print()
    print(
        f"Total: {result['total_servers']} servers, "
        f"{result['total_wrapped']} wrapped, "
        f"{result['total_unwrapped']} unwrapped."
    )


if __name__ == "__main__":
    main()

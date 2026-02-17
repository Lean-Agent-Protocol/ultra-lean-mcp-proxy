"""File watcher module for Ultra Lean MCP Proxy.

Polls MCP client config files and auto-wraps new unwrapped stdio servers.
Supports foreground and daemon modes with file locking for safe concurrent access.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .installer import (
    _ACCEPTED_URL_TRANSPORTS,
    _run_claude_mcp_command,
    acquire_config_lock,
    backup_config,
    get_config_locations,
    is_claude_cloud_scope,
    is_claude_local_scope,
    is_safe_property_name,
    is_url_bridge_available,
    is_url_server,
    is_stdio_server,
    is_wrapped,
    parse_claude_mcp_get_details,
    parse_claude_mcp_list_cloud_connectors,
    parse_claude_mcp_list_names,
    read_config,
    release_config_lock,
    resolve_proxy_path,
    wrap_entry,
    wrap_url_entry,
    write_config_atomic,
)

logger = logging.getLogger("ultra_lean_mcp_proxy.watcher")

_LOCK_RETRIES = 5
_LOCK_BACKOFF_S = 0.2
_DATA_DIR = Path.home() / ".ultra-lean-mcp-proxy"

_shutdown_requested = False


# ---------------------------------------------------------------------------
# Process utilities
# ---------------------------------------------------------------------------


def _is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    if pid <= 0:
        return False

    if platform.system() == "Windows":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except (OSError, AttributeError):
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we lack permission to signal it
            return True


def _get_pid_file_path() -> str:
    """Return path to daemon PID file."""
    return str(_DATA_DIR / "watch.pid")


def _get_log_file_path() -> str:
    """Return path to daemon log file."""
    return str(_DATA_DIR / "watch.log")


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------


def _acquire_lock(config_path: str) -> bool:
    """Acquire lock via installer shared lock helpers."""
    acquired = acquire_config_lock(config_path, retries=_LOCK_RETRIES, backoff_s=_LOCK_BACKOFF_S)
    if not acquired:
        logger.warning(
            "Could not acquire lock for %s after %d retries, skipping this cycle",
            config_path,
            _LOCK_RETRIES,
        )
    return acquired


def _release_lock(config_path: str) -> None:
    """Release lock file."""
    release_config_lock(config_path)


# ---------------------------------------------------------------------------
# Cloud discovery
# ---------------------------------------------------------------------------


def _discover_cloud_connectors(
    proxy_path: str,
    runtime: str,
    suffix: str,
    verbose: bool,
) -> None:
    """Discover cloud-scoped MCP connectors and mirror them locally.

    Runs `claude mcp list`, parses names, gets details for each, filters for
    cloud-scoped URL connectors, and writes them to claude-code-user config.

    All exceptions are caught and logged as warnings - never crashes the watcher.
    """
    try:
        # Run claude mcp list
        try:
            list_output = _run_claude_mcp_command(["list"])
        except Exception as exc:
            logger.warning("Cloud discovery: failed to run 'claude mcp list': %s", exc)
            return

        # Parse server names and cloud connectors
        names = parse_claude_mcp_list_names(list_output)
        cloud_connectors = parse_claude_mcp_list_cloud_connectors(list_output)
        if not names and not cloud_connectors:
            logger.debug("Cloud discovery: no server names found")
            return

        logger.debug("Cloud discovery: found %d server(s) + %d cloud connector(s) to inspect", len(names), len(cloud_connectors))

        # Collect cloud URL candidates via list-then-get flow
        candidates = []
        for name in names:
            try:
                details_output = _run_claude_mcp_command(["get", name])
                details = parse_claude_mcp_get_details(details_output)
            except Exception as exc:
                logger.debug("Cloud discovery: skipping '%s' (failed to get details: %s)", name, exc)
                continue

            # Skip local scopes
            if is_claude_local_scope(details.get("scope", "")):
                logger.debug("Cloud discovery: skipping '%s' (local scope)", name)
                continue

            # Skip unknown scopes
            if not is_claude_cloud_scope(details.get("scope", "")):
                logger.debug("Cloud discovery: skipping '%s' (unknown scope: %s)", name, details.get("scope"))
                continue

            # Check transport
            transport = (details.get("type") or "").lower()
            if transport not in _ACCEPTED_URL_TRANSPORTS:
                logger.debug("Cloud discovery: skipping '%s' (non-URL transport: %s)", name, transport)
                continue

            # Check URL present
            if not details.get("url"):
                logger.debug("Cloud discovery: skipping '%s' (missing URL)", name)
                continue

            # Build target name
            target_name = f"{name}{suffix}"
            if not is_safe_property_name(target_name):
                logger.debug("Cloud discovery: skipping '%s' (unsafe target name: %s)", name, target_name)
                continue

            # Build source entry
            source_entry = {
                "url": details["url"],
                "transport": transport,
            }
            if details.get("headers"):
                source_entry["headers"] = details["headers"]

            # Wrap it
            wrapped_entry = wrap_url_entry(source_entry, proxy_path, runtime=runtime)

            candidates.append({
                "source_name": name,
                "target_name": target_name,
                "wrapped_entry": wrapped_entry,
            })

        # Cloud connector entries parsed directly from list output
        candidate_target_names = {c["target_name"] for c in candidates}
        for cc in cloud_connectors:
            target_name = f"{cc['safe_name']}{suffix}"
            if not is_safe_property_name(target_name):
                logger.debug("Cloud discovery: skipping '%s' (unsafe target name: %s)", cc["display_name"], target_name)
                continue
            if target_name in candidate_target_names:
                logger.debug("Cloud discovery: skipping '%s' (already collected via get)", cc["display_name"])
                continue

            source_entry = {
                "url": cc["url"],
                "transport": cc["transport"],
            }
            wrapped_entry = wrap_url_entry(source_entry, proxy_path, runtime=runtime)
            candidates.append({
                "source_name": cc["display_name"],
                "target_name": target_name,
                "wrapped_entry": wrapped_entry,
            })
            candidate_target_names.add(target_name)

        if not candidates:
            logger.debug("Cloud discovery: no cloud URL connectors found")
            return

        logger.info("Cloud discovery: found %d cloud URL connector(s) to sync", len(candidates))

        # Find claude-code-user config
        locations = get_config_locations(offline=True)
        target_loc = None
        for loc in locations:
            if loc["name"] == "claude-code-user":
                target_loc = loc
                break
        if target_loc is None:
            target_loc = {
                "name": "claude-code-user",
                "path": os.path.join(str(Path.home()), ".claude.json"),
                "key": "mcpServers",
            }

        config_path = target_loc["path"]
        server_key = target_loc.get("key", "mcpServers")

        # Ensure parent dir exists
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)

        # Acquire lock
        if not acquire_config_lock(config_path):
            logger.warning("Cloud discovery: could not acquire lock for %s, skipping", config_path)
            return

        try:
            # Read config
            config = {}
            if Path(config_path).exists():
                try:
                    config = read_config(config_path)
                except Exception as exc:
                    logger.warning("Cloud discovery: failed to read %s: %s", config_path, exc)
                    return

            if not isinstance(config.get(server_key), dict):
                config[server_key] = {}
            servers = config[server_key]

            # Merge candidates
            changed = False
            for candidate in candidates:
                target_name = candidate["target_name"]
                wrapped_entry = candidate["wrapped_entry"]
                existing = servers.get(target_name)

                # Compare by JSON serialization
                if existing and json.dumps(existing, sort_keys=True) == json.dumps(wrapped_entry, sort_keys=True):
                    logger.debug("Cloud discovery: '%s' already up to date", target_name)
                    continue

                servers[target_name] = wrapped_entry
                changed = True
                logger.info("Cloud discovery: wrote '%s' -> '%s'", candidate["source_name"], target_name)

            if changed:
                config[server_key] = servers
                # Backup if file exists
                if Path(config_path).exists():
                    backup_config(config_path)
                write_config_atomic(config_path, config)
                logger.info("Cloud discovery: updated %s", config_path)
            else:
                logger.debug("Cloud discovery: no changes needed")

        finally:
            release_config_lock(config_path)

    except Exception as exc:
        logger.warning("Cloud discovery: unexpected error: %s", exc, exc_info=verbose)


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------


def _handle_shutdown(signum: int, frame: object) -> None:
    """Signal handler that sets the shutdown flag."""
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Shutdown signal received (signal %d), stopping...", signum)


def watch_configs(
    interval: float = 5.0,
    runtime: str = "pip",
    offline: bool = False,
    wrap_url: bool = True,
    verbose: bool = False,
    proxy_path: str | None = None,
    suffix: str = "-ulmp",
    cloud_interval: float = 60.0,
) -> None:
    """Main watch loop. Polls config files and auto-wraps new servers.

    Runs until SIGINT or SIGTERM is received.

    Args:
        interval: Poll interval in seconds.
        runtime: Runtime marker for wrapped entries (pip or npm).
        offline: Skip remote registry fetch when discovering configs.
        wrap_url: Wrap URL entries through bridge chain (default on).
        verbose: Enable verbose/debug logging.
        proxy_path: Explicit path to proxy binary. Resolved automatically if None.
        suffix: Suffix to append to cloud connector names (default: -ulmp).
        cloud_interval: Interval in seconds between cloud discovery runs (default: 60.0).
    """
    global _shutdown_requested
    _shutdown_requested = False

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # Resolve proxy path
    if proxy_path is None:
        proxy_path = resolve_proxy_path()
    logger.info("Proxy path: %s", proxy_path)

    # Check if cloud discovery is available
    claude_cli_available = shutil.which("claude") is not None
    if claude_cli_available:
        logger.info("Cloud auto-discovery enabled (interval: %.1fs, suffix: %s)", cloud_interval, suffix)
    else:
        logger.info("Cloud auto-discovery disabled (claude CLI not found)")

    # Discover config locations
    locations = get_config_locations(offline=offline)
    if not locations:
        logger.warning("No config locations found. Nothing to watch.")
        return

    can_wrap_url = is_url_bridge_available() if wrap_url else False
    if wrap_url and not can_wrap_url:
        logger.warning("URL wrapping enabled but `npx` not found. URL entries will be skipped.")

    logger.info(
        "Watching %d config location(s) every %.1fs: %s",
        len(locations),
        interval,
        ", ".join(loc["name"] for loc in locations),
    )

    # Track mtime for each config path
    mtime_cache: dict[str, float] = {}

    # Track cloud discovery timing
    last_cloud_check = 0.0
    if claude_cli_available:
        # Run initial cloud discovery
        logger.info("Running initial cloud discovery...")
        _discover_cloud_connectors(proxy_path, runtime, suffix, verbose)
        last_cloud_check = time.monotonic()

    while not _shutdown_requested:
        # Check if it's time to run cloud discovery
        if claude_cli_available:
            current_time = time.monotonic()
            elapsed = current_time - last_cloud_check
            if elapsed >= cloud_interval:
                logger.debug("Running periodic cloud discovery...")
                _discover_cloud_connectors(proxy_path, runtime, suffix, verbose)
                last_cloud_check = current_time

        for loc in locations:
            config_path = loc["path"]
            config_name = loc["name"]
            config_key = loc["key"]

            # Check if file exists
            try:
                stat = os.stat(config_path)
            except OSError:
                # File does not exist or is inaccessible -- skip silently
                # Remove from mtime cache if it was tracked before (file deleted)
                if config_path in mtime_cache:
                    logger.info(
                        "%s: config file no longer accessible, removing from watch",
                        config_name,
                    )
                    mtime_cache.pop(config_path, None)
                continue

            current_mtime = stat.st_mtime

            # Skip if mtime has not changed
            if config_path in mtime_cache and mtime_cache[config_path] == current_mtime:
                continue

            # First time seeing this file -- just record mtime, do not process
            if config_path not in mtime_cache:
                logger.debug("%s: initial mtime recorded", config_name)
                # Still process on first pass to catch any unwrapped servers
                # that already existed before the watcher started
                pass

            logger.debug("%s: change detected (mtime %.3f)", config_name, current_mtime)

            # Acquire lock
            if not _acquire_lock(config_path):
                continue

            try:
                # Re-read mtime after acquiring lock (may have changed)
                try:
                    stat = os.stat(config_path)
                    current_mtime = stat.st_mtime
                except OSError:
                    logger.debug(
                        "%s: file disappeared after lock acquired", config_name
                    )
                    mtime_cache.pop(config_path, None)
                    continue

                # Read config
                try:
                    data = read_config(config_path)
                except (OSError, ValueError) as exc:
                    logger.warning("%s: failed to read config: %s", config_name, exc)
                    mtime_cache[config_path] = current_mtime
                    continue

                servers = data.get(config_key, {})
                if not isinstance(servers, dict):
                    logger.debug(
                        "%s: '%s' is not a dict, skipping", config_name, config_key
                    )
                    mtime_cache[config_path] = current_mtime
                    continue

                # Wrap new unwrapped stdio servers
                changed = False
                for server_name, entry in servers.items():
                    if not isinstance(entry, dict):
                        continue
                    stdio = is_stdio_server(entry)
                    url = is_url_server(entry)
                    if not stdio and not url:
                        continue
                    if is_wrapped(entry):
                        continue
                    if url and not wrap_url:
                        continue
                    if url and not can_wrap_url:
                        logger.warning(
                            "%s: skipping '%s' URL wrap (bridge unavailable)",
                            config_name,
                            server_name,
                        )
                        continue

                    wrapped = (
                        wrap_url_entry(entry, proxy_path, runtime=runtime)
                        if url
                        else wrap_entry(entry, proxy_path, runtime=runtime)
                    )
                    servers[server_name] = wrapped
                    changed = True
                    logger.info(
                        "%s: wrapped server '%s' (%s, origin=%s)",
                        config_name,
                        server_name,
                        runtime,
                        "url" if url else "stdio",
                    )

                if changed:
                    data[config_key] = servers
                    write_config_atomic(config_path, data)
                    logger.info("%s: config updated", config_name)
                    # Re-read mtime after write
                    try:
                        stat = os.stat(config_path)
                        current_mtime = stat.st_mtime
                    except OSError:
                        pass

                mtime_cache[config_path] = current_mtime

            finally:
                _release_lock(config_path)

        # Sleep in small increments to respond to shutdown quickly
        sleep_remaining = interval
        while sleep_remaining > 0 and not _shutdown_requested:
            chunk = min(sleep_remaining, 0.5)
            time.sleep(chunk)
            sleep_remaining -= chunk

    logger.info("Watcher stopped.")


# ---------------------------------------------------------------------------
# Daemon management
# ---------------------------------------------------------------------------


def start_daemon(
    interval: float = 5.0,
    runtime: str = "pip",
    offline: bool = False,
    wrap_url: bool = True,
    verbose: bool = False,
    suffix: str = "-ulmp",
    cloud_interval: float = 60.0,
) -> None:
    """Start watcher as a background daemon.

    On Unix: forks and detaches via os.setsid().
    On Windows: launches a subprocess with CREATE_NO_WINDOW.

    Writes the daemon PID to ~/.ultra-lean-mcp-proxy/watch.pid and
    redirects output to ~/.ultra-lean-mcp-proxy/watch.log.
    """
    pid_file = _get_pid_file_path()
    log_file = _get_log_file_path()

    # Check if a daemon is already running
    if os.path.isfile(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8") as f:
                existing_pid = int(f.read().strip())
            if _is_process_alive(existing_pid):
                print(
                    f"Watcher daemon is already running (PID {existing_pid}).",
                    file=sys.stderr,
                )
                sys.exit(1)
            else:
                # Stale PID file -- remove it
                os.unlink(pid_file)
        except (OSError, ValueError):
            # Corrupt PID file -- remove it
            try:
                os.unlink(pid_file)
            except OSError:
                pass

    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    if platform.system() == "Windows":
        _start_daemon_windows(interval, runtime, offline, wrap_url, verbose, pid_file, log_file, suffix, cloud_interval)
    else:
        _start_daemon_unix(interval, runtime, offline, wrap_url, verbose, pid_file, log_file, suffix, cloud_interval)


def _start_daemon_unix(
    interval: float,
    runtime: str,
    offline: bool,
    wrap_url: bool,
    verbose: bool,
    pid_file: str,
    log_file: str,
    suffix: str,
    cloud_interval: float,
) -> None:
    """Fork and detach on Unix."""
    pid = os.fork()
    if pid > 0:
        # Parent: report and exit
        print(f"Watcher daemon started (PID {pid}).", file=sys.stderr)
        print(f"  Log file: {log_file}", file=sys.stderr)
        print(f"  PID file: {pid_file}", file=sys.stderr)
        return

    # Child: detach
    os.setsid()

    # Second fork to fully detach
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Grandchild: this is the daemon process
    daemon_pid = os.getpid()

    # Write PID file
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(daemon_pid))

    # Redirect stdout/stderr to log file
    log_fd = open(log_file, "a", encoding="utf-8")
    os.dup2(log_fd.fileno(), sys.stdout.fileno())
    os.dup2(log_fd.fileno(), sys.stderr.fileno())

    # Close stdin
    devnull = open(os.devnull, "r")
    os.dup2(devnull.fileno(), sys.stdin.fileno())

    # Set up logging for daemon
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )

    try:
        watch_configs(
            interval=interval,
            runtime=runtime,
            offline=offline,
            wrap_url=wrap_url,
            verbose=verbose,
            suffix=suffix,
            cloud_interval=cloud_interval,
        )
    finally:
        try:
            os.unlink(pid_file)
        except OSError:
            pass


def _start_daemon_windows(
    interval: float,
    runtime: str,
    offline: bool,
    wrap_url: bool,
    verbose: bool,
    pid_file: str,
    log_file: str,
    suffix: str,
    cloud_interval: float,
) -> None:
    """Launch a detached subprocess on Windows."""
    # Build the command to run the watcher in foreground mode
    cmd = [
        sys.executable,
        "-m",
        "ultra_lean_mcp_proxy.cli",
        "watch",
        "--interval",
        str(interval),
        "--runtime",
        runtime,
        "--suffix",
        suffix,
        "--cloud-interval",
        str(cloud_interval),
    ]
    if offline:
        cmd.append("--offline")
    if not wrap_url:
        cmd.append("--no-wrap-url")
    if verbose:
        cmd.append("--verbose")

    CREATE_NO_WINDOW = 0x08000000

    with open(log_file, "a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )

    # Write PID file
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    print(f"Watcher daemon started (PID {proc.pid}).", file=sys.stderr)
    print(f"  Log file: {log_file}", file=sys.stderr)
    print(f"  PID file: {pid_file}", file=sys.stderr)


def stop_daemon() -> None:
    """Stop running daemon by reading PID file and sending SIGTERM.

    On Unix, sends SIGTERM. On Windows, uses TerminateProcess.
    Deletes the PID file after stopping the daemon.
    """
    pid_file = _get_pid_file_path()

    if not os.path.isfile(pid_file):
        print("No watcher daemon is running (PID file not found).", file=sys.stderr)
        sys.exit(1)

    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError) as exc:
        print(f"Failed to read PID file: {exc}", file=sys.stderr)
        sys.exit(1)

    if not _is_process_alive(pid):
        print(
            f"Watcher daemon (PID {pid}) is not running. Cleaning up PID file.",
            file=sys.stderr,
        )
        try:
            os.unlink(pid_file)
        except OSError:
            pass
        return

    # Terminate the process
    if platform.system() == "Windows":
        try:
            import ctypes

            PROCESS_TERMINATE = 0x0001
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                ctypes.windll.kernel32.TerminateProcess(handle, 1)
                ctypes.windll.kernel32.CloseHandle(handle)
            else:
                print(
                    f"Failed to open process {pid} for termination.",
                    file=sys.stderr,
                )
                sys.exit(1)
        except (OSError, AttributeError) as exc:
            print(f"Failed to terminate process {pid}: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            print(
                f"Permission denied when trying to stop PID {pid}.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Wait briefly for the process to terminate
    for _ in range(20):
        if not _is_process_alive(pid):
            break
        time.sleep(0.25)

    # Clean up PID file
    try:
        os.unlink(pid_file)
    except OSError:
        pass

    if _is_process_alive(pid):
        print(
            f"Warning: daemon (PID {pid}) did not terminate within 5 seconds.",
            file=sys.stderr,
        )
    else:
        print(f"Watcher daemon (PID {pid}) stopped.", file=sys.stderr)

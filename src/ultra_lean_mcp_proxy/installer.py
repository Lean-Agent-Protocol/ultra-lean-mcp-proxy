"""Installer module for Ultra Lean MCP Proxy.

Provides one-line install/uninstall of proxy wrapping across all known
MCP client configurations (Claude Desktop, Claude Code, Cursor, Windsurf, etc.).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

REMOTE_REGISTRY_URL = (
    "https://raw.githubusercontent.com/lean-agent-protocol/ultra-lean-mcp-proxy/main/registry/clients.json"
)
REMOTE_TIMEOUT_S = 3
REMOTE_MAX_BYTES = 64 * 1024  # 64 KB
DATA_DIR = Path.home() / ".ultra-lean-mcp-proxy"
ETAG_FILE = DATA_DIR / "registry-etag"
REGISTRY_CACHE_FILE = DATA_DIR / "registry-cache.json"
LOCAL_OVERRIDES_FILE = DATA_DIR / "clients.json"

ALLOWED_REGISTRY_KEYS = {"name", "paths", "key"}
SAFE_PATH_PREFIXES = ("~", "%APPDATA%", "%USERPROFILE%", "$HOME")

_SAFE_PROPERTY_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_UNSAFE_PROPERTY_NAMES = frozenset(("__proto__", "constructor", "prototype"))
_CLAUDE_LOCAL_SCOPE_PATTERN = re.compile(r"\b(local|user|project)\s+config\b", re.IGNORECASE)
_CLAUDE_CLOUD_SCOPE_PATTERN = re.compile(r"\bcloud\b", re.IGNORECASE)
_ACCEPTED_URL_TRANSPORTS = frozenset(("sse", "http", "streamable-http"))


# ---------------------------------------------------------------------------
# Config discovery
# ---------------------------------------------------------------------------


def _get_default_config_locations() -> list[dict]:
    """Return hardcoded client config paths for the current platform."""
    system = platform.system()
    home = str(Path.home())

    locations: list[dict] = []

    # Claude Desktop
    if system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        locations.append(
            {
                "name": "claude-desktop",
                "path": os.path.join(appdata, "Claude", "claude_desktop_config.json"),
                "key": "mcpServers",
            }
        )
    elif system == "Darwin":
        locations.append(
            {
                "name": "claude-desktop",
                "path": os.path.join(home, "Library", "Application Support", "Claude", "claude_desktop_config.json"),
                "key": "mcpServers",
            }
        )
    else:
        locations.append(
            {
                "name": "claude-desktop",
                "path": os.path.join(home, ".config", "claude", "claude_desktop_config.json"),
                "key": "mcpServers",
            }
        )

    # Claude Code
    locations.append(
        {
            "name": "claude-code",
            "path": os.path.join(home, ".claude", "settings.json"),
            "key": "mcpServers",
        }
    )

    # Claude Code (local)
    locations.append(
        {
            "name": "claude-code-local",
            "path": os.path.join(home, ".claude", "settings.local.json"),
            "key": "mcpServers",
        }
    )

    # Claude Code (new user config used by `claude mcp add --scope user/local`)
    locations.append(
        {
            "name": "claude-code-user",
            "path": os.path.join(home, ".claude.json"),
            "key": "mcpServers",
        }
    )

    # Cursor
    if system == "Windows":
        userprofile = os.environ.get("USERPROFILE", home)
        locations.append(
            {
                "name": "cursor",
                "path": os.path.join(userprofile, ".cursor", "mcp.json"),
                "key": "mcpServers",
            }
        )
    else:
        locations.append(
            {
                "name": "cursor",
                "path": os.path.join(home, ".cursor", "mcp.json"),
                "key": "mcpServers",
            }
        )

    # Windsurf
    if system == "Windows":
        userprofile = os.environ.get("USERPROFILE", home)
        locations.append(
            {
                "name": "windsurf",
                "path": os.path.join(userprofile, ".codeium", "windsurf", "mcp_config.json"),
                "key": "mcpServers",
            }
        )
    else:
        locations.append(
            {
                "name": "windsurf",
                "path": os.path.join(home, ".codeium", "windsurf", "mcp_config.json"),
                "key": "mcpServers",
            }
        )

    return locations


def _is_safe_path(raw_path: str) -> bool:
    """Validate that a registry path template is safe (no traversal, within home)."""
    if ".." in raw_path:
        return False
    # Reject control characters
    if any(ord(c) < 32 for c in raw_path):
        return False
    # Must start with an allowed prefix
    return any(raw_path.startswith(prefix) for prefix in SAFE_PATH_PREFIXES)


def _expand_path(raw_path: str) -> str:
    """Expand environment variables and ~ in a path template."""
    expanded = raw_path
    expanded = expanded.replace("%APPDATA%", os.environ.get("APPDATA", ""))
    expanded = expanded.replace("%USERPROFILE%", os.environ.get("USERPROFILE", str(Path.home())))
    expanded = expanded.replace("$HOME", str(Path.home()))
    expanded = os.path.expanduser(expanded)
    return expanded


def _validate_registry_entry(entry: dict) -> bool:
    """Validate a single registry entry has the correct schema."""
    if not isinstance(entry, dict):
        return False
    # Only known keys
    if not set(entry.keys()).issubset(ALLOWED_REGISTRY_KEYS):
        return False
    # Required keys
    if "name" not in entry or "paths" not in entry:
        return False
    if not isinstance(entry["name"], str) or not entry["name"]:
        return False
    if not isinstance(entry["paths"], dict):
        return False
    # Validate all paths
    for _plat, path_val in entry["paths"].items():
        if not isinstance(path_val, str):
            return False
        if not _is_safe_path(path_val):
            return False
    return True


def _fetch_remote_registry() -> list[dict]:
    """Fetch remote client registry with ETag caching. Fails silently."""
    def _parse_registry_payload(payload: object) -> list[dict]:
        # Support both versioned format {"version": 1, "clients": [...]} and bare list.
        if isinstance(payload, dict):
            clients = payload.get("clients", [])
        elif isinstance(payload, list):
            clients = payload
        else:
            logger.debug("Remote registry has unexpected type, ignoring.")
            return []
        if not isinstance(clients, list):
            logger.debug("Remote registry clients is not a list, ignoring.")
            return []

        system = platform.system().lower()
        platform_key = {"windows": "win32", "darwin": "darwin", "linux": "linux"}.get(system, "linux")

        results: list[dict] = []
        for entry in clients:
            if not _validate_registry_entry(entry):
                logger.debug("Skipping invalid registry entry: %s", entry.get("name", "<unknown>"))
                continue

            path_template = entry["paths"].get(platform_key)
            if not path_template:
                continue

            results.append(
                {
                    "name": entry["name"],
                    "path": _expand_path(path_template),
                    "key": entry.get("key", "mcpServers"),
                }
            )

        return results

    def _load_cached_registry() -> list[dict]:
        if not REGISTRY_CACHE_FILE.exists():
            return []
        try:
            cached_raw = REGISTRY_CACHE_FILE.read_text(encoding="utf-8")
            cached_payload = json.loads(cached_raw)
            return _parse_registry_payload(cached_payload)
        except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.debug("Failed to load cached registry payload: %s", exc)
            return []

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        headers = {}
        if ETAG_FILE.exists():
            try:
                stored_etag = ETAG_FILE.read_text(encoding="utf-8").strip()
                if stored_etag:
                    headers["If-None-Match"] = stored_etag
            except OSError:
                pass

        req = Request(REMOTE_REGISTRY_URL, headers=headers)
        try:
            resp = urlopen(req, timeout=REMOTE_TIMEOUT_S)  # noqa: S310
        except HTTPError as exc:
            if exc.code == 304:
                return _load_cached_registry()
            raise

        if resp.status == 304:
            return _load_cached_registry()

        raw = resp.read(REMOTE_MAX_BYTES + 1)
        if len(raw) > REMOTE_MAX_BYTES:
            logger.debug("Remote registry exceeds max payload size, ignoring.")
            return []

        # Save ETag + raw payload cache (for future 304 responses).
        etag = resp.headers.get("ETag", "")
        if etag:
            try:
                ETAG_FILE.write_text(etag, encoding="utf-8")
            except OSError:
                pass
        try:
            REGISTRY_CACHE_FILE.write_text(raw.decode("utf-8"), encoding="utf-8")
        except OSError:
            pass

        data = json.loads(raw.decode("utf-8"))
        return _parse_registry_payload(data)

    except (URLError, OSError, json.JSONDecodeError, ValueError, KeyError, HTTPError) as exc:
        logger.debug("Remote registry fetch failed (expected in offline environments): %s", exc)
        return []


def _load_local_overrides() -> list[dict]:
    """Load user's local client overrides from ~/.ultra-lean-mcp-proxy/clients.json."""
    if not LOCAL_OVERRIDES_FILE.exists():
        return []

    try:
        raw = LOCAL_OVERRIDES_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, list):
            logger.warning("Local overrides file is not a list, ignoring.")
            return []

        system = platform.system().lower()
        platform_key = {"windows": "win32", "darwin": "darwin", "linux": "linux"}.get(system, "linux")

        results: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if "name" not in entry:
                continue

            # Accept either a direct "path" or platform-specific "paths"
            if "path" in entry:
                path = entry["path"]
                if isinstance(path, str):
                    results.append(
                        {
                            "name": entry["name"],
                            "path": _expand_path(path),
                            "key": entry.get("key", "mcpServers"),
                        }
                    )
            elif "paths" in entry and isinstance(entry["paths"], dict):
                path_template = entry["paths"].get(platform_key)
                if path_template and isinstance(path_template, str):
                    results.append(
                        {
                            "name": entry["name"],
                            "path": _expand_path(path_template),
                            "key": entry.get("key", "mcpServers"),
                        }
                    )

        return results

    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load local overrides: %s", exc)
        return []


def get_config_locations(offline: bool = False) -> list[dict]:
    """Merge all three config sources. Each dict has: name, path, key.

    Priority: local overrides > remote registry > hardcoded defaults.
    Remote entries only add/update, never remove hardcoded defaults.
    """
    defaults = _get_default_config_locations()
    remote = [] if offline else _fetch_remote_registry()
    local = _load_local_overrides()

    # Start with defaults, keyed by name
    by_name: dict[str, dict] = {}
    for loc in defaults:
        by_name[loc["name"]] = loc

    # Merge remote (add or update, never remove)
    for loc in remote:
        by_name[loc["name"]] = loc

    # Merge local overrides (highest priority)
    for loc in local:
        by_name[loc["name"]] = loc

    return list(by_name.values())


# ---------------------------------------------------------------------------
# JSONC parser
# ---------------------------------------------------------------------------


def strip_jsonc_comments(text: str) -> str:
    """Strip // and /* */ comments from JSONC using a state machine.

    Correctly handles // inside string values (e.g. URLs) by tracking
    whether we are inside a JSON string.
    """
    result: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    escape = False

    while i < length:
        ch = text[i]

        if in_string:
            result.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        # Not in a string
        if ch == '"':
            in_string = True
            result.append(ch)
            i += 1
            continue

        # Check for // line comment
        if ch == "/" and i + 1 < length and text[i + 1] == "/":
            # Skip until end of line
            i += 2
            while i < length and text[i] != "\n":
                i += 1
            continue

        # Check for /* block comment */
        if ch == "/" and i + 1 < length and text[i + 1] == "*":
            i += 2
            while i + 1 < length and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2  # skip */
            continue

        result.append(ch)
        i += 1

    return "".join(result)


def read_config(path: str) -> dict:
    """Read a config file, handling JSONC comments."""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        stripped = strip_jsonc_comments(raw)
        return json.loads(stripped)


# ---------------------------------------------------------------------------
# Structural detection
# ---------------------------------------------------------------------------


def is_stdio_server(entry: dict) -> bool:
    """Check if entry uses stdio transport (has command, no url)."""
    return "command" in entry and "url" not in entry


def is_url_server(entry: dict) -> bool:
    """Check if entry uses URL-based transport (http/sse/streamable-http)."""
    return isinstance(entry, dict) and isinstance(entry.get("url"), str) and bool(entry.get("url"))


def _escape_cmd_arg(value: str) -> str:
    """Escape cmd.exe metacharacters for a single token argument."""
    escaped = []
    for ch in str(value):
        if ch in "^&|<>()!":
            escaped.append("^")
        escaped.append(ch)
    return "".join(escaped)


def _bridge_command_for_url(url: str) -> list[str]:
    target = str(url).strip()
    if platform.system() == "Windows":
        # cmd.exe interprets metacharacters inside URL query strings (e.g. '&').
        # Escape them so the URL remains a single literal argument.
        return ["cmd", "/c", "npx", "-y", "mcp-remote", _escape_cmd_arg(target)]
    return ["npx", "-y", "mcp-remote", target]


def is_url_bridge_available() -> bool:
    """Return True when URL bridge dependency is available locally."""
    return shutil.which("npx") is not None


# ---------------------------------------------------------------------------
# Claude CLI parsing
# ---------------------------------------------------------------------------


def is_safe_property_name(name: str) -> bool:
    """Validate that a name is safe for use as a dict key.

    Rejects prototype pollution vectors and invalid characters.
    """
    if not isinstance(name, str) or not name:
        return False
    if name in _UNSAFE_PROPERTY_NAMES:
        return False
    return bool(_SAFE_PROPERTY_NAME_PATTERN.match(name))


def parse_claude_mcp_list_names(output: str) -> list[str]:
    """Parse server names from ``claude mcp list`` output.

    Filters duplicates and names that fail ``is_safe_property_name``.
    """
    names: list[str] = []
    seen: set[str] = set()
    for raw_line in str(output or "").splitlines():
        line = raw_line.rstrip()
        match = re.match(r"^([^:\r\n]+):\s+", line)
        if not match:
            continue
        name = match.group(1).strip()
        if not name or name in seen:
            continue
        if not is_safe_property_name(name):
            continue
        seen.add(name)
        names.append(name)
    return names


def _sanitize_cloud_connector_name(display_name: str) -> str:
    """Convert a cloud connector display name to a safe property name.

    "claude.ai Canva" -> "canva"
    "claude.ai Some Service" -> "some-service"
    """
    # Strip "claude.ai " prefix (case-insensitive)
    cleaned = re.sub(r"^claude\.ai\s+", "", display_name.strip(), flags=re.IGNORECASE)
    # Lowercase, replace spaces with hyphens, strip non-alphanumeric except hyphens
    cleaned = cleaned.lower().strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"[^a-z0-9-]", "", cleaned)
    # Remove leading/trailing hyphens
    cleaned = cleaned.strip("-")
    return cleaned


_CLOUD_CONNECTOR_LINE_PATTERN = re.compile(
    r"^(claude\.ai\s+[^:]+):\s+(https?://\S+)\s+-\s+", re.IGNORECASE
)


def parse_claude_mcp_list_cloud_connectors(output: str) -> list[dict]:
    """Parse cloud connector entries directly from ``claude mcp list`` output.

    Cloud connectors have names like "claude.ai Canva" which fail
    ``is_safe_property_name`` (spaces). This parser extracts them directly
    from the list output, which already contains the URL.

    Returns list of dicts with keys: display_name, safe_name, url, scope, transport.
    """
    results: list[dict] = []
    seen: set[str] = set()
    for raw_line in str(output or "").splitlines():
        line = raw_line.rstrip()
        match = _CLOUD_CONNECTOR_LINE_PATTERN.match(line)
        if not match:
            continue
        display_name = match.group(1).strip()
        url = match.group(2).strip()
        safe_name = _sanitize_cloud_connector_name(display_name)
        if not safe_name or safe_name in seen:
            continue
        if not is_safe_property_name(safe_name):
            continue
        seen.add(safe_name)
        results.append({
            "display_name": display_name,
            "safe_name": safe_name,
            "url": url,
            "scope": "cloud",
            "transport": "sse",
        })
    return results


def parse_claude_mcp_get_details(output: str) -> dict:
    """Parse details from ``claude mcp get <name>`` output.

    Returns dict with keys: scope, type, url, command, args, headers.
    """
    info: dict = {
        "scope": None,
        "type": None,
        "url": None,
        "command": None,
        "args": None,
        "headers": {},
    }

    in_headers = False
    for raw_line in str(output or "").splitlines():
        line = raw_line.rstrip()

        m = re.match(r"^\s{2}Scope:\s*(.+)$", line)
        if m:
            info["scope"] = m.group(1).strip()
            in_headers = False
            continue

        m = re.match(r"^\s{2}Type:\s*(.+)$", line)
        if m:
            info["type"] = m.group(1).strip().lower()
            in_headers = False
            continue

        m = re.match(r"^\s{2}URL:\s*(.+)$", line)
        if m:
            info["url"] = m.group(1).strip()
            in_headers = False
            continue

        m = re.match(r"^\s{2}Command:\s*(.+)$", line)
        if m:
            info["command"] = m.group(1).strip()
            in_headers = False
            continue

        m = re.match(r"^\s{2}Args:\s*(.*)$", line)
        if m:
            info["args"] = m.group(1).strip()
            in_headers = False
            continue

        if re.match(r"^\s{2}Headers:\s*$", line):
            in_headers = True
            continue

        if not in_headers:
            continue

        m = re.match(r"^\s{4}([^:]+):\s*(.*)$", line)
        if m:
            info["headers"][m.group(1).strip()] = m.group(2).strip()
            continue

        if not line.strip():
            continue

        in_headers = False

    return info


def is_claude_local_scope(scope_label: str) -> bool:
    """Return True if scope matches local/user/project."""
    return bool(_CLAUDE_LOCAL_SCOPE_PATTERN.search(str(scope_label or "").strip()))


def is_claude_cloud_scope(scope_label: str) -> bool:
    """Return True if scope is a cloud scope (positive match)."""
    normalized = str(scope_label or "").strip()
    if not normalized:
        return False
    if is_claude_local_scope(normalized):
        return False
    return bool(_CLAUDE_CLOUD_SCOPE_PATTERN.search(normalized))


def _clean_env_for_claude() -> dict[str, str]:
    """Return a copy of os.environ without keys that block nested Claude CLI calls."""
    blocked = {"CLAUDECODE", "CLAUDE_CODE"}
    return {k: v for k, v in os.environ.items() if k not in blocked}


def _run_claude_mcp_command(args: list[str]) -> str:
    """Run ``claude mcp <args>`` and return stdout.

    Raises RuntimeError on failure.
    """
    result = subprocess.run(
        ["claude", "mcp", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=_clean_env_for_claude(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"'claude mcp {' '.join(args)}' failed: {detail or f'exit code {result.returncode}'}"
        )
    return result.stdout or ""


# ---------------------------------------------------------------------------
# wrap-cloud
# ---------------------------------------------------------------------------


def wrap_cloud(
    dry_run: bool = False,
    runtime: str = "pip",
    suffix: str = "-ulmp",
    verbose: bool = False,
    _command_exists: object = None,
    _run_command: object = None,
    _resolve_proxy: object = None,
) -> dict:
    """Wrap cloud-scoped Claude MCP URL connectors by mirroring them locally.

    Returns a summary dict with keys: inspected, candidates, written, updated,
    unchanged, skipped, config_path.
    """
    if not isinstance(suffix, str) or not suffix:
        raise ValueError("--suffix must be a non-empty string")

    selected_runtime = "pip" if runtime == "pip" else "npm"

    check_cmd = _command_exists if _command_exists else lambda name: shutil.which(name) is not None
    run_cmd = _run_command if _run_command else _run_claude_mcp_command
    resolve_proxy = _resolve_proxy if _resolve_proxy else resolve_proxy_path

    if not check_cmd("claude"):
        raise RuntimeError("`claude` CLI was not found on PATH. Install Claude Code CLI first.")

    proxy_path = resolve_proxy()
    list_output = run_cmd(["list"])
    names = parse_claude_mcp_list_names(list_output)
    cloud_connectors = parse_claude_mcp_list_cloud_connectors(list_output)

    if not names and not cloud_connectors:
        if list_output.strip():
            logger.warning(
                "[wrap-cloud] `claude mcp list` produced output but no server names "
                "were parsed. The CLI output format may have changed."
            )
        print("[wrap-cloud] No Claude MCP servers found.")
        return {
            "inspected": 0,
            "candidates": 0,
            "written": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "config_path": None,
        }

    candidates = []
    skipped = 0

    # --- Existing list-then-get flow for local/standard servers ---
    for name in names:
        try:
            details = parse_claude_mcp_get_details(run_cmd(["get", name]))
        except Exception as exc:
            skipped += 1
            if verbose:
                print(f"[wrap-cloud]   {name}: skipped (failed to inspect: {exc})")
            continue

        if is_claude_local_scope(details["scope"]):
            skipped += 1
            if verbose:
                print(f"[wrap-cloud]   {name}: skipped (scope is local/user/project)")
            continue

        if not is_claude_cloud_scope(details["scope"]):
            skipped += 1
            if verbose:
                print(f"[wrap-cloud]   {name}: skipped (unknown scope: {details['scope'] or 'empty'})")
            continue

        transport = (details.get("type") or "").lower()
        if transport not in _ACCEPTED_URL_TRANSPORTS:
            skipped += 1
            if verbose:
                print(f"[wrap-cloud]   {name}: skipped (cloud scope but non-URL transport: {transport or 'unknown'})")
            continue

        if not details.get("url"):
            skipped += 1
            if verbose:
                print(f"[wrap-cloud]   {name}: skipped (cloud URL connector missing URL in CLI output)")
            continue

        target_name = f"{name}{suffix}"
        if not is_safe_property_name(target_name):
            skipped += 1
            if verbose:
                print(f'[wrap-cloud]   {name}: skipped (target name "{target_name}" is not a safe property name)')
            continue

        source_entry: dict = {
            "url": details["url"],
            "transport": transport,
        }
        if details.get("headers"):
            source_entry["headers"] = details["headers"]

        candidates.append(
            {
                "source_name": name,
                "target_name": target_name,
                "scope": details["scope"],
                "wrapped_entry": wrap_url_entry(source_entry, proxy_path, selected_runtime),
            }
        )

    # --- Cloud connector entries parsed directly from list output ---
    candidate_target_names = {c["target_name"] for c in candidates}
    for cc in cloud_connectors:
        target_name = f"{cc['safe_name']}{suffix}"
        if not is_safe_property_name(target_name):
            skipped += 1
            if verbose:
                print(f"[wrap-cloud]   {cc['display_name']}: skipped (target name \"{target_name}\" is not safe)")
            continue
        if target_name in candidate_target_names:
            if verbose:
                print(f"[wrap-cloud]   {cc['display_name']}: skipped (already collected via get)")
            continue

        source_entry = {
            "url": cc["url"],
            "transport": cc["transport"],
        }
        candidates.append(
            {
                "source_name": cc["display_name"],
                "target_name": target_name,
                "scope": cc["scope"],
                "wrapped_entry": wrap_url_entry(source_entry, proxy_path, selected_runtime),
            }
        )
        candidate_target_names.add(target_name)

    inspected_count = len(names) + len(cloud_connectors)

    if not candidates:
        print("[wrap-cloud] No cloud-scoped URL MCP servers found to wrap.")
        return {
            "inspected": inspected_count,
            "candidates": 0,
            "written": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": skipped,
            "config_path": None,
        }

    # Find claude-code-user config location
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

    if not acquire_config_lock(config_path):
        raise RuntimeError(f"config is locked by another process: {config_path}")

    written = 0
    updated = 0
    unchanged = 0

    try:
        config: dict = {}
        if Path(config_path).exists():
            try:
                config = read_config(config_path)
            except Exception:
                raise RuntimeError(f"could not parse target config: {config_path}")

        if not isinstance(config.get(server_key), dict):
            config[server_key] = {}
        servers = config[server_key]

        for candidate in candidates:
            existed = candidate["target_name"] in servers
            existing = servers.get(candidate["target_name"])
            if existing and json.dumps(existing, sort_keys=True) == json.dumps(
                candidate["wrapped_entry"], sort_keys=True
            ):
                unchanged += 1
                print(
                    f"[wrap-cloud]   {candidate['source_name']} -> {candidate['target_name']}: already up to date"
                )
                continue

            if dry_run:
                label = "Would update" if existed else "Would create"
                print(f"[wrap-cloud]   {candidate['source_name']} -> {candidate['target_name']}: {label}")
            else:
                servers[candidate["target_name"]] = candidate["wrapped_entry"]
                label = "Updated" if existed else "Created"
                print(f"[wrap-cloud]   {candidate['source_name']} -> {candidate['target_name']}: {label}")

            if existed:
                updated += 1
            else:
                written += 1

        if not dry_run and (written > 0 or updated > 0):
            if Path(config_path).exists():
                backup_config(config_path)
            write_config_atomic(config_path, config)
            print(f"[wrap-cloud]   Config saved: {config_path}")

    finally:
        release_config_lock(config_path)

    print("")
    print(
        f"[wrap-cloud] Done. Inspected: {inspected_count}, Cloud URL candidates: {len(candidates)}, "
        f"Created: {written}, Updated: {updated}, Unchanged: {unchanged}, Skipped: {skipped}"
    )
    if dry_run:
        print("[wrap-cloud] (dry run - no files were modified)")

    return {
        "inspected": inspected_count,
        "candidates": len(candidates),
        "written": written,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": skipped,
        "config_path": config_path,
    }


def _arg_before_separator(args: list, flag_name: str) -> str | None:
    if not isinstance(args, list):
        return None
    try:
        sep_idx = args.index("--")
    except ValueError:
        return None
    for i in range(1, sep_idx):
        if args[i] == flag_name and i + 1 < sep_idx:
            return str(args[i + 1])
    return None


def get_wrapped_transport(entry: dict) -> str | None:
    """Return wrapped entry origin transport (stdio/url)."""
    if not is_wrapped(entry):
        return None
    args = entry.get("args", [])
    transport = _arg_before_separator(args, "--wrapped-transport")
    return transport or "stdio"


def _encode_wrapped_entry(entry: dict) -> str:
    raw = json.dumps(entry, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _decode_wrapped_entry(payload: str) -> dict | None:
    try:
        raw = base64.b64decode(payload.encode("ascii"), validate=False)
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, json.JSONDecodeError):
        return None
    return None


def is_wrapped(entry: dict) -> bool:
    """Structural detection of a proxy-wrapped entry.

    An entry is wrapped if ALL four conditions hold:
    1. args[0] is "proxy"
    2. args contains "--runtime" followed by a value ("pip" or "npm") before "--"
    3. args contains "--" separator
    4. At least one arg after "--"
    """
    args = entry.get("args", [])
    if not isinstance(args, list) or len(args) < 1:
        return False

    # Condition 1: first arg is "proxy"
    if args[0] != "proxy":
        return False

    # Find "--" separator
    try:
        sep_idx = args.index("--")
    except ValueError:
        return False

    # Condition 4: at least one arg after "--"
    if sep_idx >= len(args) - 1:
        return False

    # Condition 2: "--runtime" followed by a value before "--"
    proxy_args = args[1:sep_idx]
    found_runtime = False
    for j, arg in enumerate(proxy_args):
        if arg == "--runtime" and j + 1 < len(proxy_args):
            runtime_val = proxy_args[j + 1]
            if runtime_val in ("pip", "npm"):
                found_runtime = True
                break
    if not found_runtime:
        return False

    return True


def get_runtime(entry: dict) -> str | None:
    """Extract the runtime marker from a wrapped entry."""
    args = entry.get("args", [])
    if not isinstance(args, list):
        return None
    try:
        sep_idx = args.index("--")
    except ValueError:
        return None

    proxy_args = args[1:sep_idx]
    for j, arg in enumerate(proxy_args):
        if arg == "--runtime" and j + 1 < len(proxy_args):
            return proxy_args[j + 1]
    return None


# ---------------------------------------------------------------------------
# Wrap / unwrap
# ---------------------------------------------------------------------------


def wrap_entry(entry: dict, proxy_path: str, runtime: str = "pip") -> dict:
    """Wrap a single MCP server entry to route through the proxy.

    Idempotent: if the entry is already wrapped, returns it unchanged.
    """
    if is_wrapped(entry):
        return entry

    original_command = entry["command"]
    original_args = entry.get("args", [])
    if not isinstance(original_args, list):
        original_args = []

    new_entry = dict(entry)
    new_entry["command"] = proxy_path
    new_entry["args"] = ["proxy", "--runtime", runtime, "--", original_command] + list(original_args)
    return new_entry


def wrap_url_entry(entry: dict, proxy_path: str, runtime: str = "pip") -> dict:
    """Wrap a URL MCP server entry through local stdio bridge + proxy."""
    if is_wrapped(entry):
        return entry
    if not is_url_server(entry):
        return entry

    original = json.loads(json.dumps(entry))
    encoded_original = _encode_wrapped_entry(original)
    bridge_args = _bridge_command_for_url(str(entry["url"]))

    new_entry = dict(entry)
    new_entry["command"] = proxy_path
    new_entry["args"] = [
        "proxy",
        "--runtime",
        runtime,
        "--wrapped-transport",
        "url",
        "--wrapped-entry-b64",
        encoded_original,
        "--",
        *bridge_args,
    ]
    new_entry.pop("url", None)
    new_entry.pop("transport", None)
    return new_entry


def unwrap_entry(entry: dict) -> dict:
    """Unwrap a proxy-wrapped entry back to the original command."""
    args = entry.get("args", [])
    wrapped_transport = _arg_before_separator(args, "--wrapped-transport")
    wrapped_payload = _arg_before_separator(args, "--wrapped-entry-b64")
    if wrapped_transport == "url" and wrapped_payload:
        restored = _decode_wrapped_entry(wrapped_payload)
        if isinstance(restored, dict):
            return restored

    try:
        sep_idx = args.index("--")
    except ValueError:
        return entry

    after_sep = args[sep_idx + 1 :]
    if not after_sep:
        return entry

    original_command = after_sep[0]
    original_args = after_sep[1:]

    new_entry = dict(entry)
    new_entry["command"] = original_command
    new_entry["args"] = original_args
    return new_entry


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _is_process_alive(pid: int) -> bool:
    """Check whether the given PID is alive."""
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except (OSError, AttributeError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def acquire_config_lock(config_path: str, retries: int = 5, backoff_s: float = 0.2) -> bool:
    """Acquire lock file for a config path using O_CREAT|O_EXCL semantics."""
    lock_path = config_path + ".lock"
    for attempt in range(retries):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                owner_text = Path(lock_path).read_text(encoding="utf-8").strip()
                owner_pid = int(owner_text)
                if not _is_process_alive(owner_pid):
                    try:
                        os.unlink(lock_path)
                    except OSError:
                        pass
                    continue
            except (OSError, ValueError):
                pass
            if attempt < retries - 1:
                time.sleep(backoff_s)
        except OSError:
            if attempt < retries - 1:
                time.sleep(backoff_s)
    return False


def release_config_lock(config_path: str) -> None:
    """Release config lock if held."""
    lock_path = config_path + ".lock"
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def write_config_atomic(path: str, data: dict) -> None:
    """Atomic write with Windows retry on locked files."""
    tmp_path = path + ".tmp"
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    Path(tmp_path).write_text(content, encoding="utf-8")

    max_retries = 3 if platform.system() == "Windows" else 1
    for attempt in range(max_retries):
        try:
            os.replace(tmp_path, path)
            return
        except OSError:
            if attempt < max_retries - 1:
                time.sleep(0.1)
            else:
                raise


def backup_config(path: str) -> str:
    """Backup a config file to a sibling .ultra-lean-mcp-proxy-backups/ directory.

    Returns the backup file path.
    """
    config_path = Path(path)
    backup_dir = config_path.parent / ".ultra-lean-mcp-proxy-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"{config_path.stem}.{timestamp}.bak"
    backup_path = backup_dir / backup_name

    shutil.copy2(path, str(backup_path))
    return str(backup_path)


# ---------------------------------------------------------------------------
# Proxy resolution
# ---------------------------------------------------------------------------


def resolve_proxy_path() -> str:
    """Resolve absolute path to the ultra-lean-mcp-proxy binary.

    Tries shutil.which("ultra-lean-mcp-proxy"), then on Windows also tries
    the .cmd variant.
    """
    path = shutil.which("ultra-lean-mcp-proxy")
    if path:
        return path

    if platform.system() == "Windows":
        path = shutil.which("ultra-lean-mcp-proxy.cmd")
        if path:
            return path

    raise FileNotFoundError(
        "Could not find 'ultra-lean-mcp-proxy' on PATH. "
        "Make sure it is installed and available in your shell. "
        "You can install it with: pip install ultra-lean-mcp-proxy"
    )


# ---------------------------------------------------------------------------
# Install / Uninstall / Status
# ---------------------------------------------------------------------------


def install(
    dry_run: bool = False,
    client_filter: str | None = None,
    skip_names: list[str] | None = None,
    offline: bool = False,
    wrap_url: bool = True,
    verbose: bool = False,
    runtime: str = "pip",
) -> dict:
    """Install proxy wrapping on all discovered configs.

    Returns a summary dict with keys:
        configs: list of per-config results
        total_found: total servers found
        total_wrapped: total servers wrapped
    """
    proxy_path = resolve_proxy_path()
    locations = get_config_locations(offline=offline)

    if client_filter:
        locations = [loc for loc in locations if loc["name"] == client_filter]

    skip_set = set(skip_names) if skip_names else set()
    can_wrap_url = is_url_bridge_available() if wrap_url else False
    if wrap_url and not can_wrap_url:
        logger.warning(
            "URL wrapping is enabled but `npx` was not found. URL entries will be skipped."
        )

    summary: dict = {"configs": [], "total_found": 0, "total_wrapped": 0}

    for loc in locations:
        config_result: dict = {
            "name": loc["name"],
            "path": loc["path"],
            "backup": None,
            "servers": [],
            "error": None,
        }

        if not Path(loc["path"]).exists():
            config_result["error"] = "config file not found"
            summary["configs"].append(config_result)
            if verbose:
                logger.info("Skipping %s: config file not found at %s", loc["name"], loc["path"])
            continue

        if not acquire_config_lock(loc["path"]):
            config_result["error"] = "config file locked"
            summary["configs"].append(config_result)
            continue

        try:
            if not Path(loc["path"]).exists():
                config_result["error"] = "config file not found"
                summary["configs"].append(config_result)
                continue

            try:
                data = read_config(loc["path"])
            except (OSError, json.JSONDecodeError) as exc:
                config_result["error"] = f"failed to read config: {exc}"
                summary["configs"].append(config_result)
                continue

            key = loc["key"]
            servers = data.get(key, {})
            if not isinstance(servers, dict):
                config_result["error"] = f"'{key}' is not a dict"
                summary["configs"].append(config_result)
                continue

            changed = False
            for server_name, entry in servers.items():
                if not isinstance(entry, dict):
                    continue

                summary["total_found"] += 1

                if server_name in skip_set:
                    config_result["servers"].append(
                        {"name": server_name, "action": "skipped", "reason": "skip list"}
                    )
                    continue

                stdio = is_stdio_server(entry)
                url = is_url_server(entry)
                if not stdio and not url:
                    config_result["servers"].append(
                        {"name": server_name, "action": "skipped", "reason": "non-wrappable"}
                    )
                    continue

                if is_wrapped(entry):
                    existing_runtime = get_runtime(entry)
                    config_result["servers"].append(
                        {"name": server_name, "action": "skipped", "reason": f"already wrapped ({existing_runtime})"}
                    )
                    continue

                if url and not wrap_url:
                    config_result["servers"].append(
                        {"name": server_name, "action": "skipped", "reason": "url wrapping disabled"}
                    )
                    continue

                if url and not can_wrap_url:
                    config_result["servers"].append(
                        {"name": server_name, "action": "skipped", "reason": "url bridge unavailable"}
                    )
                    continue

                wrapped = wrap_url_entry(entry, proxy_path, runtime=runtime) if url else wrap_entry(
                    entry, proxy_path, runtime=runtime
                )
                servers[server_name] = wrapped
                changed = True
                summary["total_wrapped"] += 1
                config_result["servers"].append(
                    {
                        "name": server_name,
                        "action": "wrapped",
                        "runtime": runtime,
                        "origin": "url" if url else "stdio",
                    }
                )

            if changed and not dry_run:
                backup_path = backup_config(loc["path"])
                config_result["backup"] = backup_path
                data[key] = servers
                write_config_atomic(loc["path"], data)
            elif changed and dry_run:
                config_result["backup"] = "(dry-run)"

            summary["configs"].append(config_result)
        finally:
            release_config_lock(loc["path"])

    return summary


def uninstall(
    dry_run: bool = False,
    client_filter: str | None = None,
    all_runtimes: bool = False,
    runtime: str = "pip",
    verbose: bool = False,
) -> dict:
    """Uninstall proxy wrapping from all discovered configs.

    By default only unwraps entries with the requested runtime marker.
    Use all_runtimes=True
    to unwrap regardless of runtime marker.

    Returns a summary dict.
    """
    locations = get_config_locations(offline=True)

    if client_filter:
        locations = [loc for loc in locations if loc["name"] == client_filter]

    summary: dict = {"configs": [], "total_found": 0, "total_unwrapped": 0}

    for loc in locations:
        config_result: dict = {
            "name": loc["name"],
            "path": loc["path"],
            "backup": None,
            "servers": [],
            "error": None,
        }

        if not Path(loc["path"]).exists():
            config_result["error"] = "config file not found"
            summary["configs"].append(config_result)
            continue

        if not acquire_config_lock(loc["path"]):
            config_result["error"] = "config file locked"
            summary["configs"].append(config_result)
            continue

        try:
            if not Path(loc["path"]).exists():
                config_result["error"] = "config file not found"
                summary["configs"].append(config_result)
                continue

            try:
                data = read_config(loc["path"])
            except (OSError, json.JSONDecodeError) as exc:
                config_result["error"] = f"failed to read config: {exc}"
                summary["configs"].append(config_result)
                continue

            key = loc["key"]
            servers = data.get(key, {})
            if not isinstance(servers, dict):
                config_result["error"] = f"'{key}' is not a dict"
                summary["configs"].append(config_result)
                continue

            changed = False
            for server_name, entry in servers.items():
                if not isinstance(entry, dict):
                    continue

                summary["total_found"] += 1

                if not is_wrapped(entry):
                    config_result["servers"].append(
                        {"name": server_name, "action": "skipped", "reason": "not wrapped"}
                    )
                    continue

                entry_runtime = get_runtime(entry)
                if not all_runtimes and entry_runtime != runtime:
                    config_result["servers"].append(
                        {"name": server_name, "action": "skipped", "reason": f"different runtime ({entry_runtime})"}
                    )
                    continue

                unwrapped = unwrap_entry(entry)
                servers[server_name] = unwrapped
                changed = True
                summary["total_unwrapped"] += 1
                config_result["servers"].append(
                    {"name": server_name, "action": "unwrapped", "runtime": entry_runtime}
                )

            if changed and not dry_run:
                backup_path = backup_config(loc["path"])
                config_result["backup"] = backup_path
                data[key] = servers
                write_config_atomic(loc["path"], data)
            elif changed and dry_run:
                config_result["backup"] = "(dry-run)"

            summary["configs"].append(config_result)
        finally:
            release_config_lock(loc["path"])

    return summary


def status() -> list[dict] | dict:
    """Show current status of all discovered configs.

    Returns a list of per-client status dicts. Each dict has:
        name, path, exists, servers (list), error.
    Each server entry has: name, wrapped (bool), status, runtime (if wrapped).

    Also accessible as a dict via status_summary() for CLI formatting.
    """
    locations = get_config_locations(offline=True)

    results: list[dict] = []

    for loc in locations:
        config_result: dict = {
            "name": loc["name"],
            "path": loc["path"],
            "exists": False,
            "servers": [],
            "error": None,
        }

        if not Path(loc["path"]).exists():
            results.append(config_result)
            continue

        config_result["exists"] = True

        try:
            data = read_config(loc["path"])
        except (OSError, json.JSONDecodeError) as exc:
            config_result["error"] = f"failed to read config: {exc}"
            results.append(config_result)
            continue

        key = loc["key"]
        servers = data.get(key, {})
        if not isinstance(servers, dict):
            config_result["error"] = f"'{key}' is not a dict"
            results.append(config_result)
            continue

        for server_name, entry in servers.items():
            if not isinstance(entry, dict):
                continue

            if is_wrapped(entry):
                entry_runtime = get_runtime(entry)
                origin = get_wrapped_transport(entry) or "stdio"
                config_result["servers"].append(
                    {
                        "name": server_name,
                        "wrapped": True,
                        "status": "wrapped",
                        "runtime": entry_runtime,
                        "origin": origin,
                    }
                )
            elif is_stdio_server(entry):
                config_result["servers"].append(
                    {
                        "name": server_name,
                        "wrapped": False,
                        "status": "unwrapped",
                        "runtime": None,
                        "origin": "stdio",
                    }
                )
            elif is_url_server(entry):
                config_result["servers"].append(
                    {
                        "name": server_name,
                        "wrapped": False,
                        "status": "remote-unwrapped",
                        "runtime": None,
                        "origin": "url",
                    }
                )
            else:
                config_result["servers"].append(
                    {
                        "name": server_name,
                        "wrapped": False,
                        "status": "non-stdio",
                        "runtime": None,
                        "origin": None,
                    }
                )

        results.append(config_result)

    return results


def status_summary() -> dict:
    """Show current status with aggregate counts for CLI display.

    Returns a dict with keys: configs (list), total_servers, total_wrapped, total_unwrapped.
    """
    results = status()

    total_servers = 0
    total_wrapped = 0
    total_unwrapped = 0

    for cfg in results:
        for srv in cfg.get("servers", []):
            total_servers += 1
            if srv.get("wrapped"):
                total_wrapped += 1
            elif srv.get("status") in {"unwrapped", "remote-unwrapped"}:
                total_unwrapped += 1

    return {
        "configs": results,
        "total_servers": total_servers,
        "total_wrapped": total_wrapped,
        "total_unwrapped": total_unwrapped,
    }

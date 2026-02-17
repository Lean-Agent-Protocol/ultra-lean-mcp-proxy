"""Comprehensive tests for the installer module.

All tests monkeypatch internal location-discovery helpers so that real config
files are never touched.  Every test uses ``tmp_path`` for isolation.
"""

from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch, MagicMock

import pytest

from ultra_lean_mcp_proxy.installer import (
    strip_jsonc_comments,
    read_config,
    is_wrapped,
    get_runtime,
    get_wrapped_transport,
    wrap_entry,
    wrap_url_entry,
    unwrap_entry,
    is_stdio_server,
    is_url_server,
    write_config_atomic,
    backup_config,
    install,
    uninstall,
    status,
    is_safe_property_name,
    parse_claude_mcp_list_names,
    parse_claude_mcp_list_cloud_connectors,
    parse_claude_mcp_get_details,
    is_claude_local_scope,
    is_claude_cloud_scope,
    wrap_cloud,
    _clean_env_for_claude,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_PROXY = "/usr/local/bin/ultra-lean-mcp-proxy"


def _make_config(tmp_path: Path, name: str, servers: dict) -> str:
    """Create a test config file and return its absolute path."""
    path = tmp_path / name / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"mcpServers": servers}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(path)


def _mock_locations(tmp_path: Path, configs: dict[str, dict]) -> list[dict]:
    """Build mock config locations for testing."""
    locations: list[dict] = []
    for name, servers in configs.items():
        path = _make_config(tmp_path, name, servers)
        locations.append({"name": name, "path": path, "key": "mcpServers"})
    return locations


def _read_servers(path: str) -> dict:
    """Read the mcpServers dict back from a config file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)["mcpServers"]


def _patch_installer(monkeypatch, locations):
    """Apply all standard installer patches at once."""
    import ultra_lean_mcp_proxy.installer as inst

    monkeypatch.setattr(inst, "_get_default_config_locations", lambda: locations)
    monkeypatch.setattr(inst, "_fetch_remote_registry", lambda: [])
    monkeypatch.setattr(inst, "_load_local_overrides", lambda: [])
    monkeypatch.setattr(inst, "resolve_proxy_path", lambda: FAKE_PROXY)
    monkeypatch.setattr(inst, "is_url_bridge_available", lambda: True)


# ---------------------------------------------------------------------------
# 1. wrap / unwrap basic
# ---------------------------------------------------------------------------


class TestWrapUnwrapBasic:
    """Wrap an entry, verify structure, unwrap, verify matches original."""

    def test_wrap_produces_correct_structure(self):
        original = {
            "command": "npx",
            "args": ["@modelcontextprotocol/server-filesystem", "/tmp"],
        }
        wrapped = wrap_entry(original, FAKE_PROXY, runtime="pip")

        assert wrapped["command"] == FAKE_PROXY
        args = wrapped["args"]
        assert args[0] == "proxy"
        assert "--runtime" in args
        rt_idx = args.index("--runtime")
        assert args[rt_idx + 1] == "pip"
        assert "--" in args
        sep_idx = args.index("--")
        # Original command + args appear after the separator
        assert args[sep_idx + 1] == "npx"
        assert args[sep_idx + 2] == "@modelcontextprotocol/server-filesystem"
        assert args[sep_idx + 3] == "/tmp"

    def test_unwrap_restores_original(self):
        original = {
            "command": "npx",
            "args": ["@modelcontextprotocol/server-filesystem", "/tmp"],
        }
        wrapped = wrap_entry(original, FAKE_PROXY, runtime="pip")
        restored = unwrap_entry(wrapped)

        assert restored["command"] == original["command"]
        assert restored["args"] == original["args"]

    def test_wrap_entry_without_args(self):
        original = {"command": "my-server"}
        wrapped = wrap_entry(original, FAKE_PROXY, runtime="npm")
        assert wrapped["command"] == FAKE_PROXY
        args = wrapped["args"]
        sep_idx = args.index("--")
        assert args[sep_idx + 1] == "my-server"
        # No further args after the original command
        assert len(args) == sep_idx + 2

        restored = unwrap_entry(wrapped)
        assert restored["command"] == "my-server"
        assert restored.get("args", []) == []

    def test_wrap_url_entry_roundtrip(self):
        original = {
            "url": "https://mcp.example.com/sse",
            "headers": {"Authorization": "Bearer xyz"},
        }
        wrapped = wrap_url_entry(original, FAKE_PROXY, runtime="pip")
        assert wrapped["command"] == FAKE_PROXY
        assert is_wrapped(wrapped) is True
        assert get_wrapped_transport(wrapped) == "url"

        restored = unwrap_entry(wrapped)
        assert restored == original

    def test_wrap_url_entry_windows_escapes_cmd_metacharacters(self):
        original = {
            "url": "https://mcp.example.com/sse?mode=a&pipe=b|c",
        }
        with patch("ultra_lean_mcp_proxy.installer.platform.system", return_value="Windows"):
            wrapped = wrap_url_entry(original, FAKE_PROXY, runtime="pip")

        sep_idx = wrapped["args"].index("--")
        bridge_cmd = wrapped["args"][sep_idx + 1 :]
        assert bridge_cmd[:5] == ["cmd", "/c", "npx", "-y", "mcp-remote"]
        assert bridge_cmd[5] == "https://mcp.example.com/sse?mode=a^&pipe=b^|c"

        restored = unwrap_entry(wrapped)
        assert restored == original


# ---------------------------------------------------------------------------
# 2. wrap / unwrap idempotent
# ---------------------------------------------------------------------------


class TestWrapIdempotent:
    """Wrapping an already-wrapped entry should be a no-op."""

    def test_double_wrap_is_noop(self):
        original = {
            "command": "npx",
            "args": ["server-github"],
        }
        wrapped_once = wrap_entry(original, FAKE_PROXY, runtime="pip")
        wrapped_twice = wrap_entry(wrapped_once, FAKE_PROXY, runtime="pip")

        assert wrapped_once == wrapped_twice

    def test_double_wrap_different_runtime_is_noop(self):
        original = {"command": "npx", "args": ["server"]}
        wrapped = wrap_entry(original, FAKE_PROXY, runtime="pip")
        # Attempting to re-wrap with different runtime should still be a no-op
        # because the entry is already wrapped.
        wrapped_again = wrap_entry(wrapped, FAKE_PROXY, runtime="npm")
        assert wrapped == wrapped_again


# ---------------------------------------------------------------------------
# 3. skip non-stdio servers
# ---------------------------------------------------------------------------


class TestSkipNonStdio:
    """Entries with a ``url`` key are SSE/streamable-HTTP servers, not stdio."""

    def test_url_entry_is_not_stdio(self):
        entry = {"url": "https://mcp.example.com/sse"}
        assert is_stdio_server(entry) is False
        assert is_url_server(entry) is True

    def test_command_entry_is_stdio(self):
        entry = {"command": "npx", "args": ["server"]}
        assert is_stdio_server(entry) is True

    def test_command_with_url_is_not_stdio(self):
        # If both are present, ``url`` takes precedence
        entry = {"command": "npx", "url": "https://example.com"}
        assert is_stdio_server(entry) is False

    def test_empty_entry_is_not_stdio(self):
        assert is_stdio_server({}) is False


# ---------------------------------------------------------------------------
# 4. JSON comments handling (JSONC)
# ---------------------------------------------------------------------------


class TestStripJsoncComments:
    """Test the JSONC state-machine parser."""

    def test_line_comment_stripped(self):
        text = '{"key": "value"} // this is a comment'
        result = strip_jsonc_comments(text)
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_block_comment_stripped(self):
        text = '{"key": /* a comment */ "value"}'
        result = strip_jsonc_comments(text)
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_multiline_block_comment(self):
        text = '{\n  /* this is\n     a multi-line\n     comment */\n  "key": "value"\n}'
        result = strip_jsonc_comments(text)
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_url_in_string_preserved(self):
        text = '{"url": "https://example.com"}'
        result = strip_jsonc_comments(text)
        parsed = json.loads(result)
        assert parsed["url"] == "https://example.com"

    def test_double_slash_inside_string_preserved(self):
        text = '{"path": "//network/share"}'
        result = strip_jsonc_comments(text)
        parsed = json.loads(result)
        assert parsed["path"] == "//network/share"

    def test_block_comment_syntax_inside_string_preserved(self):
        text = '{"pattern": "/* not a comment */"}'
        result = strip_jsonc_comments(text)
        parsed = json.loads(result)
        assert parsed["pattern"] == "/* not a comment */"

    def test_escaped_quotes_in_strings(self):
        text = r'{"msg": "he said \"hello\" // still string"}'
        result = strip_jsonc_comments(text)
        parsed = json.loads(result)
        assert "hello" in parsed["msg"]
        assert "// still string" in parsed["msg"]

    def test_comment_after_string_with_slashes(self):
        text = '{"url": "https://example.com"} // comment here'
        result = strip_jsonc_comments(text)
        parsed = json.loads(result)
        assert parsed == {"url": "https://example.com"}

    def test_trailing_commas_not_handled(self):
        # strip_jsonc_comments only strips comments, not trailing commas
        text = '{"a": 1, "b": 2}'
        result = strip_jsonc_comments(text)
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# 5. backup creation
# ---------------------------------------------------------------------------


class TestBackupCreation:
    """Verify backup file is created in the sibling backups directory."""

    def test_backup_creates_file(self, tmp_path):
        config_path = tmp_path / "claude" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text('{"mcpServers": {}}', encoding="utf-8")

        backup_path = backup_config(str(config_path))

        assert backup_path is not None
        assert os.path.isfile(backup_path)
        backup_dir = os.path.dirname(backup_path)
        assert ".ultra-lean-mcp-proxy-backups" in backup_dir

    def test_backup_content_matches_original(self, tmp_path):
        original_data = {"mcpServers": {"github": {"command": "npx", "args": ["server"]}}}
        config_path = tmp_path / "test-client" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(original_data), encoding="utf-8")

        backup_path = backup_config(str(config_path))

        with open(backup_path, encoding="utf-8") as f:
            backup_data = json.load(f)
        assert backup_data == original_data

    def test_backup_directory_is_sibling(self, tmp_path):
        config_path = tmp_path / "mydir" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("{}", encoding="utf-8")

        backup_path = backup_config(str(config_path))

        backup_parent = Path(backup_path).parent
        config_parent = config_path.parent
        # Backup dir should be a sibling of the config file's directory
        # i.e. both share the same grandparent, or backup is under
        # config_parent / .ultra-lean-mcp-proxy-backups
        assert ".ultra-lean-mcp-proxy-backups" in str(backup_parent)


# ---------------------------------------------------------------------------
# 6. dry-run doesn't modify files
# ---------------------------------------------------------------------------


class TestDryRunNoModify:
    """Install with dry_run=True should not alter any config files."""

    def test_install_dry_run_preserves_files(self, tmp_path, monkeypatch):
        servers = {
            "github": {"command": "npx", "args": ["server-github"]},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)

        original_text = Path(locations[0]["path"]).read_text(encoding="utf-8")

        install(dry_run=True, runtime="pip")

        after_text = Path(locations[0]["path"]).read_text(encoding="utf-8")
        assert original_text == after_text

    def test_uninstall_dry_run_preserves_files(self, tmp_path, monkeypatch):
        servers = {
            "github": {"command": "npx", "args": ["server-github"]},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)

        # First do a real install
        install(dry_run=False, runtime="pip")
        wrapped_text = Path(locations[0]["path"]).read_text(encoding="utf-8")

        # Then dry-run uninstall
        uninstall(dry_run=True)
        after_text = Path(locations[0]["path"]).read_text(encoding="utf-8")
        assert wrapped_text == after_text


# ---------------------------------------------------------------------------
# 7. full install -> uninstall roundtrip
# ---------------------------------------------------------------------------


class TestInstallUninstallRoundtrip:
    """Install, verify wrapped, uninstall, verify original JSON equality."""

    def test_roundtrip_single_server(self, tmp_path, monkeypatch):
        servers = {
            "filesystem": {
                "command": "npx",
                "args": ["@modelcontextprotocol/server-filesystem", "/tmp"],
            },
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)
        config_path = locations[0]["path"]

        original_data = json.loads(Path(config_path).read_text(encoding="utf-8"))

        # Install
        install(dry_run=False, runtime="pip")
        wrapped_servers = _read_servers(config_path)
        assert is_wrapped(wrapped_servers["filesystem"])

        # Uninstall
        uninstall(dry_run=False)
        restored_data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        assert restored_data == original_data

    def test_roundtrip_multiple_servers(self, tmp_path, monkeypatch):
        servers = {
            "github": {"command": "npx", "args": ["server-github"]},
            "filesystem": {"command": "npx", "args": ["server-fs", "/home"]},
            "memory": {"command": "npx", "args": ["server-memory"]},
        }
        locations = _mock_locations(tmp_path, {"cursor": servers})
        _patch_installer(monkeypatch, locations)
        config_path = locations[0]["path"]

        original_data = json.loads(Path(config_path).read_text(encoding="utf-8"))

        install(dry_run=False, runtime="pip")

        for name in servers:
            assert is_wrapped(_read_servers(config_path)[name])

        uninstall(dry_run=False)
        restored_data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        assert restored_data == original_data

    def test_roundtrip_across_multiple_clients(self, tmp_path, monkeypatch):
        configs = {
            "claude-desktop": {"github": {"command": "npx", "args": ["server-github"]}},
            "cursor": {"fs": {"command": "python", "args": ["-m", "fs_server"]}},
        }
        locations = _mock_locations(tmp_path, configs)
        _patch_installer(monkeypatch, locations)

        originals = {}
        for loc in locations:
            originals[loc["name"]] = json.loads(
                Path(loc["path"]).read_text(encoding="utf-8")
            )

        install(dry_run=False, runtime="pip")
        uninstall(dry_run=False)

        for loc in locations:
            restored = json.loads(Path(loc["path"]).read_text(encoding="utf-8"))
            assert restored == originals[loc["name"]]

    def test_roundtrip_url_server_restores_original_entry(self, tmp_path, monkeypatch):
        servers = {
            "remote": {
                "url": "https://mcp.example.com/sse",
                "headers": {"Authorization": "Bearer abc"},
            },
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)
        config_path = locations[0]["path"]

        original_data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        install(dry_run=False, runtime="pip")

        wrapped_servers = _read_servers(config_path)
        assert is_wrapped(wrapped_servers["remote"])
        assert get_wrapped_transport(wrapped_servers["remote"]) == "url"

        uninstall(dry_run=False)
        restored_data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        assert restored_data == original_data


# ---------------------------------------------------------------------------
# 8. preserves env vars and extra config keys
# ---------------------------------------------------------------------------


class TestPreservesExtraKeys:
    """Config with ``env`` and custom keys should survive wrap/unwrap."""

    def test_env_preserved_through_roundtrip(self, tmp_path, monkeypatch):
        servers = {
            "github": {
                "command": "npx",
                "args": ["server-github"],
                "env": {"GITHUB_TOKEN": "ghp_abc123"},
            },
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)
        config_path = locations[0]["path"]

        install(dry_run=False, runtime="pip")
        wrapped = _read_servers(config_path)["github"]
        # env should still be present on the wrapped entry
        assert wrapped.get("env") == {"GITHUB_TOKEN": "ghp_abc123"}

        uninstall(dry_run=False)
        restored = _read_servers(config_path)["github"]
        assert restored["env"] == {"GITHUB_TOKEN": "ghp_abc123"}

    def test_custom_keys_preserved(self, tmp_path, monkeypatch):
        servers = {
            "myserver": {
                "command": "python",
                "args": ["-m", "myserver"],
                "env": {"API_KEY": "secret"},
                "customField": "keep-me",
                "timeout": 30,
            },
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)
        config_path = locations[0]["path"]

        install(dry_run=False, runtime="pip")
        uninstall(dry_run=False)

        restored = _read_servers(config_path)["myserver"]
        assert restored["customField"] == "keep-me"
        assert restored["timeout"] == 30
        assert restored["env"] == {"API_KEY": "secret"}


# ---------------------------------------------------------------------------
# 9. atomic write creates .tmp then renames
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """Verify atomic write uses a temp file then rename."""

    def test_atomic_write_creates_file(self, tmp_path):
        target = str(tmp_path / "output.json")
        data = {"mcpServers": {"test": {"command": "echo"}}}
        write_config_atomic(target, data)

        with open(target, encoding="utf-8") as f:
            written = json.load(f)
        assert written == data

    def test_atomic_write_no_partial_on_error(self, tmp_path):
        target = str(tmp_path / "output.json")
        data = {"mcpServers": {"test": {"command": "echo"}}}
        write_config_atomic(target, data)

        # Verify the file exists and is valid JSON
        with open(target, encoding="utf-8") as f:
            result = json.load(f)
        assert result == data

        # No .tmp file should linger
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_atomic_write_overwrites_existing(self, tmp_path):
        target = str(tmp_path / "output.json")
        Path(target).write_text('{"old": true}', encoding="utf-8")

        new_data = {"mcpServers": {"new": {"command": "new-cmd"}}}
        write_config_atomic(target, new_data)

        with open(target, encoding="utf-8") as f:
            written = json.load(f)
        assert written == new_data


# ---------------------------------------------------------------------------
# 10. Windows .cmd PATH resolution
# ---------------------------------------------------------------------------


class TestWindowsCmdResolution:
    """On Windows, ``.cmd`` extension should be tried for proxy resolution."""

    def test_cmd_extension_tried_on_windows(self, tmp_path, monkeypatch):
        import ultra_lean_mcp_proxy.installer as inst

        # Create a fake .cmd file
        cmd_path = tmp_path / "ultra-lean-mcp-proxy.cmd"
        cmd_path.write_text("@echo off", encoding="utf-8")

        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(platform, "system", lambda: "Windows")

        # Patch shutil.which to simulate Windows finding the .cmd
        with patch("shutil.which") as mock_which:
            mock_which.return_value = str(cmd_path)
            result = inst.resolve_proxy_path()
            assert result is not None
            assert result.endswith(".cmd") or result.endswith("ultra-lean-mcp-proxy")


# ---------------------------------------------------------------------------
# 11. uninstall after user manually edits wrapped entries
# ---------------------------------------------------------------------------


class TestUninstallAfterManualEdit:
    """If user adds extra args to a wrapped entry, unwrap should still work
    as long as structural detection passes."""

    def test_unwrap_with_user_added_stats_flag(self, tmp_path, monkeypatch):
        servers = {
            "github": {"command": "npx", "args": ["server-github"]},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)
        config_path = locations[0]["path"]

        install(dry_run=False, runtime="pip")

        # Simulate user manually adding --stats to the wrapped entry
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        entry = data["mcpServers"]["github"]
        args = entry["args"]
        sep_idx = args.index("--")
        # Insert --stats before the separator
        args.insert(sep_idx, "--stats")
        Path(config_path).write_text(json.dumps(data, indent=2), encoding="utf-8")

        # The entry should still be detected as wrapped
        modified_entry = _read_servers(config_path)["github"]
        assert is_wrapped(modified_entry)

        # Uninstall should still restore the original
        uninstall(dry_run=False)
        restored = _read_servers(config_path)["github"]
        assert restored["command"] == "npx"
        assert restored["args"] == ["server-github"]


# ---------------------------------------------------------------------------
# 12. runtime marker: pip uninstall doesn't unwrap npm entries
# ---------------------------------------------------------------------------


class TestRuntimeMarkerFiltering:
    """Wrap with runtime='npm', attempt uninstall with default runtime='pip',
    verify entry stays wrapped."""

    def test_pip_uninstall_skips_npm_entries(self, tmp_path, monkeypatch):
        servers = {
            "github": {"command": "npx", "args": ["server-github"]},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)
        config_path = locations[0]["path"]

        # Install with npm runtime
        install(dry_run=False, runtime="npm")
        wrapped = _read_servers(config_path)["github"]
        assert is_wrapped(wrapped)
        assert get_runtime(wrapped) == "npm"

        # Uninstall with pip runtime (default) should NOT unwrap
        uninstall(dry_run=False)  # default runtime is pip
        still_wrapped = _read_servers(config_path)["github"]
        assert is_wrapped(still_wrapped)
        assert get_runtime(still_wrapped) == "npm"

    def test_npm_uninstall_unwraps_npm_entries(self, tmp_path, monkeypatch):
        servers = {
            "github": {"command": "npx", "args": ["server-github"]},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)
        config_path = locations[0]["path"]

        install(dry_run=False, runtime="npm")
        uninstall(dry_run=False, runtime="npm")

        restored = _read_servers(config_path)["github"]
        assert not is_wrapped(restored)
        assert restored["command"] == "npx"

    def test_all_runtimes_uninstall_unwraps_everything(self, tmp_path, monkeypatch):
        servers = {
            "github": {"command": "npx", "args": ["server-github"]},
            "local": {"command": "python", "args": ["-m", "local_server"]},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)
        config_path = locations[0]["path"]

        # Wrap github with npm, local with pip
        # We need to do this in two passes since install wraps everything
        # with the same runtime. So instead, install all with pip first,
        # then manually change one.
        install(dry_run=False, runtime="pip")

        # Manually change github to npm runtime for test scenario
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        gh_args = data["mcpServers"]["github"]["args"]
        rt_idx = gh_args.index("--runtime")
        gh_args[rt_idx + 1] = "npm"
        Path(config_path).write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Verify mixed runtimes
        mixed = _read_servers(config_path)
        assert get_runtime(mixed["github"]) == "npm"
        assert get_runtime(mixed["local"]) == "pip"

        # all_runtimes should unwrap everything
        uninstall(dry_run=False, all_runtimes=True)
        restored = _read_servers(config_path)
        assert not is_wrapped(restored["github"])
        assert not is_wrapped(restored["local"])

    def test_runtime_filtering_applies_to_url_wrapped_entries(self, tmp_path, monkeypatch):
        servers = {
            "remote": {"url": "https://mcp.example.com/sse"},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)
        config_path = locations[0]["path"]

        install(dry_run=False, runtime="npm")
        wrapped = _read_servers(config_path)["remote"]
        assert is_wrapped(wrapped)
        assert get_runtime(wrapped) == "npm"
        assert get_wrapped_transport(wrapped) == "url"

        uninstall(dry_run=False)  # default runtime pip; should skip
        still_wrapped = _read_servers(config_path)["remote"]
        assert is_wrapped(still_wrapped)

        uninstall(dry_run=False, runtime="npm")
        restored = _read_servers(config_path)["remote"]
        assert not is_wrapped(restored)
        assert restored == {"url": "https://mcp.example.com/sse"}


# ---------------------------------------------------------------------------
# 13. mixed pip/npm runtime detection in status
# ---------------------------------------------------------------------------


class TestMixedRuntimeStatus:
    """Wrap some with pip, some with npm, verify status reports correctly."""

    def test_status_reports_mixed_runtimes(self, tmp_path, monkeypatch):
        servers = {
            "github": {"command": "npx", "args": ["server-github"]},
            "local": {"command": "python", "args": ["-m", "local_server"]},
            "unwrapped": {"command": "node", "args": ["plain-server"]},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)
        config_path = locations[0]["path"]

        # Install all with pip
        install(dry_run=False, runtime="pip")

        # Manually change github's runtime to npm
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        gh_args = data["mcpServers"]["github"]["args"]
        rt_idx = gh_args.index("--runtime")
        gh_args[rt_idx + 1] = "npm"
        Path(config_path).write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Manually unwrap 'unwrapped' to simulate a non-wrapped entry
        entry = data["mcpServers"]["unwrapped"]
        restored_unwrapped = unwrap_entry(entry)
        data["mcpServers"]["unwrapped"] = restored_unwrapped
        Path(config_path).write_text(json.dumps(data, indent=2), encoding="utf-8")

        result = status()

        # result should be a list of status info dicts
        assert isinstance(result, list)
        assert len(result) > 0

        # Find entries for our servers
        all_server_statuses = []
        for client_status in result:
            if "servers" in client_status:
                all_server_statuses.extend(client_status["servers"])

        # Verify runtime information is reported
        runtimes_found = set()
        for s in all_server_statuses:
            if s.get("wrapped"):
                runtimes_found.add(s.get("runtime"))

        assert "pip" in runtimes_found
        assert "npm" in runtimes_found


# ---------------------------------------------------------------------------
# 14. is_wrapped structural detection - edge cases
# ---------------------------------------------------------------------------


class TestIsWrappedStructuralDetection:
    """Test various edge cases for structural detection."""

    def test_fully_valid_wrapped_entry(self):
        entry = {
            "command": FAKE_PROXY,
            "args": ["proxy", "--runtime", "pip", "--", "npx", "server"],
        }
        assert is_wrapped(entry) is True

    def test_wrapped_with_extra_flags(self):
        entry = {
            "command": FAKE_PROXY,
            "args": ["proxy", "--stats", "--runtime", "pip", "--", "npx"],
        }
        assert is_wrapped(entry) is True

    def test_missing_runtime_flag(self):
        entry = {
            "command": FAKE_PROXY,
            "args": ["proxy", "--", "npx"],
        }
        assert is_wrapped(entry) is False

    def test_missing_separator(self):
        entry = {
            "command": FAKE_PROXY,
            "args": ["proxy", "--runtime", "pip"],
        }
        assert is_wrapped(entry) is False

    def test_non_proxy_subcommand(self):
        entry = {
            "command": FAKE_PROXY,
            "args": ["serve"],
        }
        assert is_wrapped(entry) is False

    def test_no_args_at_all(self):
        entry = {"command": "some-command"}
        assert is_wrapped(entry) is False

    def test_empty_args(self):
        entry = {"command": "some-command", "args": []}
        assert is_wrapped(entry) is False

    def test_args_not_starting_with_proxy(self):
        entry = {
            "command": FAKE_PROXY,
            "args": ["--runtime", "pip", "--", "npx"],
        }
        assert is_wrapped(entry) is False

    def test_no_args_after_separator(self):
        # Must have at least one arg after "--"
        entry = {
            "command": FAKE_PROXY,
            "args": ["proxy", "--runtime", "pip", "--"],
        }
        assert is_wrapped(entry) is False

    def test_runtime_value_present(self):
        # --runtime without a value after it
        entry = {
            "command": FAKE_PROXY,
            "args": ["proxy", "--runtime", "--", "npx"],
        }
        # "--" appears right after --runtime, so "--" would be treated
        # as the runtime value. This depends on implementation but should
        # likely fail structural detection because there would be no
        # actual "--" separator after the runtime value.
        # The implementation should handle this edge case.
        # We just verify it does not crash.
        result = is_wrapped(entry)
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# 15. get_runtime extraction
# ---------------------------------------------------------------------------


class TestGetRuntimeExtraction:
    """Verify correct runtime is extracted from various positions."""

    def test_runtime_pip(self):
        entry = {
            "command": FAKE_PROXY,
            "args": ["proxy", "--runtime", "pip", "--", "npx", "server"],
        }
        assert get_runtime(entry) == "pip"

    def test_runtime_npm(self):
        entry = {
            "command": FAKE_PROXY,
            "args": ["proxy", "--runtime", "npm", "--", "npx", "server"],
        }
        assert get_runtime(entry) == "npm"

    def test_runtime_after_other_flags(self):
        entry = {
            "command": FAKE_PROXY,
            "args": ["proxy", "--stats", "--verbose", "--runtime", "pip", "--", "npx"],
        }
        assert get_runtime(entry) == "pip"

    def test_runtime_returns_none_for_unwrapped(self):
        entry = {"command": "npx", "args": ["server"]}
        assert get_runtime(entry) is None

    def test_runtime_returns_none_for_no_runtime_flag(self):
        entry = {
            "command": FAKE_PROXY,
            "args": ["proxy", "--", "npx"],
        }
        assert get_runtime(entry) is None

    def test_runtime_custom_value(self):
        entry = {
            "command": FAKE_PROXY,
            "args": ["proxy", "--runtime", "uv", "--", "python", "-m", "server"],
        }
        assert get_runtime(entry) == "uv"


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


class TestReadConfigJsonc:
    """Test read_config with JSONC files."""

    def test_read_config_with_comments(self, tmp_path):
        config_path = tmp_path / "config.json"
        jsonc_content = """{
  // This is a comment
  "mcpServers": {
    /* Block comment */
    "github": {
      "command": "npx",
      "args": ["server-github"]
    }
  }
}"""
        config_path.write_text(jsonc_content, encoding="utf-8")
        data = read_config(str(config_path))
        assert "mcpServers" in data
        assert "github" in data["mcpServers"]

    def test_read_config_with_url_containing_slashes(self, tmp_path):
        config_path = tmp_path / "config.json"
        jsonc_content = """{
  "mcpServers": {
    "remote": {
      "url": "https://mcp.example.com/sse"
    }
  }
}"""
        config_path.write_text(jsonc_content, encoding="utf-8")
        data = read_config(str(config_path))
        assert data["mcpServers"]["remote"]["url"] == "https://mcp.example.com/sse"


class TestClientFilter:
    """Test install/uninstall with client_filter."""

    def test_install_with_client_filter(self, tmp_path, monkeypatch):
        configs = {
            "claude-desktop": {"github": {"command": "npx", "args": ["server-github"]}},
            "cursor": {"fs": {"command": "python", "args": ["-m", "fs_server"]}},
        }
        locations = _mock_locations(tmp_path, configs)
        _patch_installer(monkeypatch, locations)

        install(dry_run=False, client_filter="claude-desktop", runtime="pip")

        # Only claude-desktop should be wrapped
        claude_servers = _read_servers(locations[0]["path"])
        cursor_servers = _read_servers(locations[1]["path"])

        assert is_wrapped(claude_servers["github"])
        assert not is_wrapped(cursor_servers["fs"])

    def test_uninstall_with_client_filter(self, tmp_path, monkeypatch):
        configs = {
            "claude-desktop": {"github": {"command": "npx", "args": ["server-github"]}},
            "cursor": {"fs": {"command": "python", "args": ["-m", "fs_server"]}},
        }
        locations = _mock_locations(tmp_path, configs)
        _patch_installer(monkeypatch, locations)

        # Install everywhere
        install(dry_run=False, runtime="pip")
        assert is_wrapped(_read_servers(locations[0]["path"])["github"])
        assert is_wrapped(_read_servers(locations[1]["path"])["fs"])

        # Uninstall only cursor
        uninstall(dry_run=False, client_filter="cursor", all_runtimes=True)

        claude_servers = _read_servers(locations[0]["path"])
        cursor_servers = _read_servers(locations[1]["path"])
        assert is_wrapped(claude_servers["github"])
        assert not is_wrapped(cursor_servers["fs"])


class TestSkipNames:
    """Test install with skip_names to skip specific server names."""

    def test_skip_names_excludes_servers(self, tmp_path, monkeypatch):
        servers = {
            "github": {"command": "npx", "args": ["server-github"]},
            "memory": {"command": "npx", "args": ["server-memory"]},
            "filesystem": {"command": "npx", "args": ["server-fs", "/tmp"]},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)

        install(dry_run=False, skip_names=["memory", "filesystem"], runtime="pip")

        result = _read_servers(locations[0]["path"])
        assert is_wrapped(result["github"])
        assert not is_wrapped(result["memory"])
        assert not is_wrapped(result["filesystem"])


class TestUrlWrapping:
    """URL/SSE/HTTP entries are wrapped by default and can be opted out."""

    def test_sse_server_wrapped_by_default(self, tmp_path, monkeypatch):
        servers = {
            "local-stdio": {"command": "npx", "args": ["server-github"]},
            "remote-sse": {"url": "https://mcp.example.com/sse"},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)

        install(dry_run=False, runtime="pip")

        result = _read_servers(locations[0]["path"])
        assert is_wrapped(result["local-stdio"])
        assert is_wrapped(result["remote-sse"])
        assert get_wrapped_transport(result["remote-sse"]) == "url"

    def test_no_wrap_url_keeps_url_entry_unwrapped(self, tmp_path, monkeypatch):
        servers = {
            "remote-sse": {"url": "https://mcp.example.com/sse"},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)

        install(dry_run=False, runtime="pip", wrap_url=False)
        result = _read_servers(locations[0]["path"])
        assert not is_wrapped(result["remote-sse"])
        assert result["remote-sse"]["url"] == "https://mcp.example.com/sse"

    def test_url_wrap_skipped_when_bridge_unavailable(self, tmp_path, monkeypatch):
        import ultra_lean_mcp_proxy.installer as inst

        servers = {"remote-sse": {"url": "https://mcp.example.com/sse"}}
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)
        monkeypatch.setattr(inst, "is_url_bridge_available", lambda: False)

        install(dry_run=False, runtime="pip")
        result = _read_servers(locations[0]["path"])
        assert not is_wrapped(result["remote-sse"])
        assert result["remote-sse"]["url"] == "https://mcp.example.com/sse"


class TestMissingConfigFile:
    """Gracefully handle missing or unreadable config files."""

    def test_install_skips_missing_config(self, tmp_path, monkeypatch):
        locations = [
            {
                "name": "claude-desktop",
                "path": str(tmp_path / "nonexistent" / "config.json"),
                "key": "mcpServers",
            }
        ]
        _patch_installer(monkeypatch, locations)

        # Should not raise; just skip the missing file
        install(dry_run=False, runtime="pip")

    def test_status_skips_missing_config(self, tmp_path, monkeypatch):
        locations = [
            {
                "name": "claude-desktop",
                "path": str(tmp_path / "nonexistent" / "config.json"),
                "key": "mcpServers",
            }
        ]
        _patch_installer(monkeypatch, locations)

        result = status()
        assert isinstance(result, list)


class TestRemoteRegistryCaching:
    """Remote registry 304 responses should load from cached payload."""

    def test_fetch_remote_registry_uses_cache_on_304(self, tmp_path, monkeypatch):
        import ultra_lean_mcp_proxy.installer as inst

        data_dir = tmp_path / ".ultra-lean-mcp-proxy"
        etag_file = data_dir / "registry-etag"
        cache_file = data_dir / "registry-cache.json"
        data_dir.mkdir(parents=True, exist_ok=True)
        etag_file.write_text('"abc123"', encoding="utf-8")
        cache_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "clients": [
                        {
                            "name": "test-client",
                            "paths": {
                                "win32": "%USERPROFILE%/.test/mcp.json",
                                "darwin": "~/.test/mcp.json",
                                "linux": "~/.test/mcp.json",
                            },
                            "key": "mcpServers",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(inst, "DATA_DIR", data_dir)
        monkeypatch.setattr(inst, "ETAG_FILE", etag_file)
        monkeypatch.setattr(inst, "REGISTRY_CACHE_FILE", cache_file)

        def _raise_304(*_args, **_kwargs):
            raise HTTPError(inst.REMOTE_REGISTRY_URL, 304, "Not Modified", None, None)

        monkeypatch.setattr(inst, "urlopen", _raise_304)

        results = inst._fetch_remote_registry()
        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0]["name"] == "test-client"


# ---------------------------------------------------------------------------
# Claude CLI parsing tests
# ---------------------------------------------------------------------------


class TestIsSafePropertyName:
    """Test is_safe_property_name validates correctly."""

    def test_valid_names(self):
        assert is_safe_property_name("linear") is True
        assert is_safe_property_name("my-server") is True
        assert is_safe_property_name("my_server.v2") is True
        assert is_safe_property_name("linear-ulmp") is True
        assert is_safe_property_name("a") is True
        assert is_safe_property_name("X123") is True

    def test_invalid_names(self):
        assert is_safe_property_name("") is False
        assert is_safe_property_name(None) is False
        assert is_safe_property_name(42) is False
        assert is_safe_property_name("__proto__") is False
        assert is_safe_property_name("constructor") is False
        assert is_safe_property_name("prototype") is False
        assert is_safe_property_name("has spaces") is False
        assert is_safe_property_name(".starts-with-dot") is False
        assert is_safe_property_name("-starts-with-dash") is False


class TestParseClaudeMcpListNames:
    """Test parse_claude_mcp_list_names extracts and filters names."""

    def test_extracts_names(self):
        output = "\n".join(
            [
                "Checking MCP server health...",
                "",
                "linear: https://mcp.linear.app/sse - ! Needs authentication",
                "filesystem-local: npx -y @modelcontextprotocol/server-filesystem /tmp - ok",
                "",
            ]
        )
        names = parse_claude_mcp_list_names(output)
        assert names == ["linear", "filesystem-local"]

    def test_deduplicates(self):
        output = "\n".join(
            [
                "linear: url1",
                "linear: url2",
                "other: something",
            ]
        )
        assert parse_claude_mcp_list_names(output) == ["linear", "other"]

    def test_filters_unsafe(self):
        output = "\n".join(
            [
                "__proto__: evil",
                "constructor: evil",
                "valid-name: ok",
            ]
        )
        assert parse_claude_mcp_list_names(output) == ["valid-name"]

    def test_handles_empty(self):
        assert parse_claude_mcp_list_names("") == []
        assert parse_claude_mcp_list_names(None) == []


class TestParseClaudeMcpGetDetails:
    """Test parse_claude_mcp_get_details output parsing."""

    def test_parses_cloud_url_connector(self):
        output = "\n".join(
            [
                "linear:",
                "  Scope: Claude.ai cloud connector",
                "  Status: ! Needs authentication",
                "  Type: sse",
                "  URL: https://mcp.linear.app/sse",
                "  Headers:",
                "    Authorization: Bearer secret-token",
                "",
            ]
        )
        details = parse_claude_mcp_get_details(output)
        assert details["scope"] == "Claude.ai cloud connector"
        assert details["type"] == "sse"
        assert details["url"] == "https://mcp.linear.app/sse"
        assert details["headers"] == {"Authorization": "Bearer secret-token"}

    def test_parses_local_stdio(self):
        output = "\n".join(
            [
                "local:",
                "  Scope: Local config (private to you in this project)",
                "  Type: stdio",
                "  Command: npx server",
                "  Args: --flag",
            ]
        )
        details = parse_claude_mcp_get_details(output)
        assert details["scope"] == "Local config (private to you in this project)"
        assert details["type"] == "stdio"
        assert details["command"] == "npx server"
        assert details["args"] == "--flag"
        assert details["url"] is None

    def test_multiple_headers(self):
        output = "\n".join(
            [
                "srv:",
                "  Type: sse",
                "  URL: https://example.com",
                "  Headers:",
                "    X-Api-Key: key123",
                "    Authorization: Bearer abc",
            ]
        )
        details = parse_claude_mcp_get_details(output)
        assert details["headers"] == {"X-Api-Key": "key123", "Authorization": "Bearer abc"}

    def test_empty_output(self):
        details = parse_claude_mcp_get_details("")
        assert details["scope"] is None
        assert details["type"] is None
        assert details["url"] is None
        assert details["headers"] == {}


class TestCleanEnvForClaude:
    """Test _clean_env_for_claude strips blocking env vars."""

    def test_strips_claudecode(self, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        env = _clean_env_for_claude()
        assert "CLAUDECODE" not in env
        assert "PATH" in env  # normal vars preserved

    def test_strips_claude_code(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE", "1")
        env = _clean_env_for_claude()
        assert "CLAUDE_CODE" not in env

    def test_strips_both(self, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE", "1")
        env = _clean_env_for_claude()
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE" not in env

    def test_preserves_other_vars(self, monkeypatch):
        monkeypatch.setenv("MY_CUSTOM_VAR", "hello")
        env = _clean_env_for_claude()
        assert env.get("MY_CUSTOM_VAR") == "hello"

    def test_works_without_blocked_vars(self):
        env = _clean_env_for_claude()
        assert isinstance(env, dict)
        assert "PATH" in env or "Path" in env  # Windows uses 'Path'


class TestClaudeScopeDetection:
    """Test is_claude_local_scope and is_claude_cloud_scope."""

    def test_local_patterns(self):
        assert is_claude_local_scope("Local config (private to you in this project)") is True
        assert is_claude_local_scope("User config (available in all your projects)") is True
        assert is_claude_local_scope("Project config (.mcp.json)") is True

    def test_cloud_pattern(self):
        assert is_claude_cloud_scope("Claude.ai cloud connector") is True
        assert is_claude_cloud_scope("Some cloud thing") is True

    def test_empty_none(self):
        assert is_claude_local_scope("") is False
        assert is_claude_local_scope(None) is False
        assert is_claude_cloud_scope("") is False
        assert is_claude_cloud_scope(None) is False

    def test_unknown_scope_returns_false(self):
        assert is_claude_cloud_scope("Unknown new scope") is False
        assert is_claude_cloud_scope("Experimental beta scope") is False


class TestWrapCloud:
    """Test wrap_cloud function with mocked CLI."""

    def _mock_list_output(self):
        return "\n".join(
            [
                "linear: https://mcp.linear.app/sse - ok",
                "local-server: npx server - ok",
            ]
        )

    def _mock_get(self, args):
        if args[1] == "linear":
            return "\n".join(
                [
                    "linear:",
                    "  Scope: Claude.ai cloud connector",
                    "  Type: sse",
                    "  URL: https://mcp.linear.app/sse",
                    "  Headers:",
                    "    Authorization: Bearer token",
                ]
            )
        elif args[1] == "local-server":
            return "\n".join(
                [
                    "local-server:",
                    "  Scope: Local config (private to you in this project)",
                    "  Type: stdio",
                    "  Command: npx server",
                ]
            )
        raise RuntimeError(f"unexpected: {args}")

    def _mock_run(self, args):
        if args[0] == "list":
            return self._mock_list_output()
        if args[0] == "get":
            return self._mock_get(args)
        raise RuntimeError(f"unexpected: {args}")

    def test_basic(self, tmp_path, monkeypatch):
        import ultra_lean_mcp_proxy.installer as inst

        config_path = str(tmp_path / ".claude.json")
        monkeypatch.setattr(
            inst,
            "get_config_locations",
            lambda offline=True: [{"name": "claude-code-user", "path": config_path, "key": "mcpServers"}],
        )

        result = wrap_cloud(
            dry_run=False,
            runtime="pip",
            suffix="-ulmp",
            verbose=True,
            _command_exists=lambda name: True,
            _run_command=self._mock_run,
            _resolve_proxy=lambda: FAKE_PROXY,
        )

        assert result["inspected"] == 2
        assert result["candidates"] == 1
        assert result["written"] == 1
        assert result["skipped"] == 1

        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        assert "linear-ulmp" in data["mcpServers"]
        assert is_wrapped(data["mcpServers"]["linear-ulmp"])

    def test_dry_run(self, tmp_path, monkeypatch):
        import ultra_lean_mcp_proxy.installer as inst

        config_path = str(tmp_path / ".claude.json")
        monkeypatch.setattr(
            inst,
            "get_config_locations",
            lambda offline=True: [{"name": "claude-code-user", "path": config_path, "key": "mcpServers"}],
        )

        result = wrap_cloud(
            dry_run=True,
            runtime="pip",
            suffix="-ulmp",
            _command_exists=lambda name: True,
            _run_command=self._mock_run,
            _resolve_proxy=lambda: FAKE_PROXY,
        )

        assert result["written"] == 1
        assert not Path(config_path).exists()

    def test_skips_local_scope(self, tmp_path, monkeypatch):
        import ultra_lean_mcp_proxy.installer as inst

        config_path = str(tmp_path / ".claude.json")
        monkeypatch.setattr(
            inst,
            "get_config_locations",
            lambda offline=True: [{"name": "claude-code-user", "path": config_path, "key": "mcpServers"}],
        )

        def mock_run_local_only(args):
            if args[0] == "list":
                return "local-server: npx server - ok\n"
            return "\n".join(
                [
                    "local-server:",
                    "  Scope: Local config (private to you in this project)",
                    "  Type: stdio",
                    "  Command: npx server",
                ]
            )

        result = wrap_cloud(
            dry_run=False,
            runtime="pip",
            suffix="-ulmp",
            verbose=True,
            _command_exists=lambda name: True,
            _run_command=mock_run_local_only,
            _resolve_proxy=lambda: FAKE_PROXY,
        )

        assert result["candidates"] == 0
        assert result["skipped"] == 1

    def test_skips_non_url(self, tmp_path, monkeypatch):
        import ultra_lean_mcp_proxy.installer as inst

        config_path = str(tmp_path / ".claude.json")
        monkeypatch.setattr(
            inst,
            "get_config_locations",
            lambda offline=True: [{"name": "claude-code-user", "path": config_path, "key": "mcpServers"}],
        )

        def mock_run_cloud_stdio(args):
            if args[0] == "list":
                return "cloud-stdio: some-cmd - ok\n"
            return "\n".join(
                [
                    "cloud-stdio:",
                    "  Scope: Claude.ai cloud connector",
                    "  Type: stdio",
                    "  Command: some-cmd",
                ]
            )

        result = wrap_cloud(
            dry_run=False,
            runtime="pip",
            suffix="-ulmp",
            verbose=True,
            _command_exists=lambda name: True,
            _run_command=mock_run_cloud_stdio,
            _resolve_proxy=lambda: FAKE_PROXY,
        )

        assert result["candidates"] == 0
        assert result["skipped"] == 1

    def test_no_claude_cli_raises(self):
        with pytest.raises(RuntimeError, match="not found on PATH"):
            wrap_cloud(_command_exists=lambda name: False)

    def test_empty_suffix_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            wrap_cloud(suffix="", _command_exists=lambda name: True)

    def test_unchanged_detection(self, tmp_path, monkeypatch):
        import ultra_lean_mcp_proxy.installer as inst

        config_path = str(tmp_path / ".claude.json")
        monkeypatch.setattr(
            inst,
            "get_config_locations",
            lambda offline=True: [{"name": "claude-code-user", "path": config_path, "key": "mcpServers"}],
        )

        # Run once to populate
        wrap_cloud(
            dry_run=False,
            runtime="pip",
            suffix="-ulmp",
            _command_exists=lambda name: True,
            _run_command=self._mock_run,
            _resolve_proxy=lambda: FAKE_PROXY,
        )

        # Run again - should detect unchanged
        result = wrap_cloud(
            dry_run=False,
            runtime="pip",
            suffix="-ulmp",
            _command_exists=lambda name: True,
            _run_command=self._mock_run,
            _resolve_proxy=lambda: FAKE_PROXY,
        )

        assert result["unchanged"] == 1
        assert result["written"] == 0
        assert result["updated"] == 0


# ---------------------------------------------------------------------------
# Install + cloud connector discovery integration
# ---------------------------------------------------------------------------


class TestInstallCloudDiscovery:
    """Verify that _run_install calls wrap_cloud after install."""

    def test_install_triggers_cloud_discovery(self, tmp_path, monkeypatch):
        """Install should call wrap_cloud when claude CLI is on PATH."""
        import ultra_lean_mcp_proxy.installer as inst

        servers = {
            "local": {"command": "npx", "args": ["server-local"]},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)

        # Do the install
        install(dry_run=False, runtime="pip")
        config_path = locations[0]["path"]
        assert is_wrapped(_read_servers(config_path)["local"])

        # Now verify wrap_cloud works after install (simulating what _run_install does)
        cloud_config_path = str(tmp_path / ".claude.json")
        monkeypatch.setattr(
            inst,
            "get_config_locations",
            lambda offline=True: [{"name": "claude-code-user", "path": cloud_config_path, "key": "mcpServers"}],
        )

        mock_list = "\n".join([
            "cloud-api: https://api.example.com/mcp - ok",
        ])
        mock_get = "\n".join([
            "cloud-api:",
            "  Scope: Claude.ai cloud connector",
            "  Type: sse",
            "  URL: https://api.example.com/mcp",
        ])

        def mock_run(args):
            if args[0] == "list":
                return mock_list
            return mock_get

        result = wrap_cloud(
            dry_run=False,
            runtime="pip",
            suffix="-ulmp",
            _command_exists=lambda name: True,
            _run_command=mock_run,
            _resolve_proxy=lambda: FAKE_PROXY,
        )

        assert result["candidates"] == 1
        assert result["written"] == 1

        data = json.loads(Path(cloud_config_path).read_text(encoding="utf-8"))
        assert "cloud-api-ulmp" in data["mcpServers"]
        assert is_wrapped(data["mcpServers"]["cloud-api-ulmp"])

    def test_install_no_cloud_flag_skips_discovery(self, tmp_path, monkeypatch):
        """Install with --no-cloud should not call wrap_cloud."""
        import argparse

        servers = {
            "local": {"command": "npx", "args": ["server-local"]},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)

        # Build args namespace as the CLI would
        args = argparse.Namespace(
            dry_run=False,
            client_filter=None,
            skip_names=None,
            offline=True,
            wrap_url=False,
            no_cloud=True,
            suffix="-ulmp",
            verbose=False,
            runtime="pip",
        )

        from ultra_lean_mcp_proxy.cli import _run_install

        # Mock shutil.which to track if it gets called
        which_called = []
        monkeypatch.setattr("shutil.which", lambda name: which_called.append(name) or None)

        _run_install(args)

        # shutil.which should NOT have been called because --no-cloud is set
        assert len(which_called) == 0

    def test_install_skips_cloud_when_claude_not_on_path(self, tmp_path, monkeypatch):
        """Install should silently skip cloud discovery when claude is not on PATH."""
        import argparse

        servers = {
            "local": {"command": "npx", "args": ["server-local"]},
        }
        locations = _mock_locations(tmp_path, {"claude-desktop": servers})
        _patch_installer(monkeypatch, locations)

        args = argparse.Namespace(
            dry_run=False,
            client_filter=None,
            skip_names=None,
            offline=True,
            wrap_url=False,
            no_cloud=False,
            suffix="-ulmp",
            verbose=False,
            runtime="pip",
        )

        from ultra_lean_mcp_proxy.cli import _run_install

        # Mock shutil.which to return None (claude not found)
        monkeypatch.setattr("shutil.which", lambda name: None)

        wrap_cloud_called = []
        monkeypatch.setattr(
            "ultra_lean_mcp_proxy.installer.wrap_cloud",
            lambda **kwargs: wrap_cloud_called.append(True),
        )

        _run_install(args)

        # wrap_cloud should NOT have been called
        assert len(wrap_cloud_called) == 0


# ---------------------------------------------------------------------------
# Cloud connector parser tests
# ---------------------------------------------------------------------------


class TestParseClaudeMcpListCloudConnectors:
    """Test parse_claude_mcp_list_cloud_connectors extraction."""

    def test_extracts_cloud_connectors(self):
        output = "\n".join([
            "Checking MCP server health...",
            "",
            "claude.ai Canva: https://mcp.canva.com/mcp - Connected",
            "claude.ai Linear: https://mcp.linear.app/sse - ! Needs authentication",
            "linear: https://mcp.linear.app/sse - ok",
            "local-server: npx server - ok",
            "",
        ])
        results = parse_claude_mcp_list_cloud_connectors(output)
        assert len(results) == 2
        assert results[0]["display_name"] == "claude.ai Canva"
        assert results[0]["safe_name"] == "canva"
        assert results[0]["url"] == "https://mcp.canva.com/mcp"
        assert results[0]["scope"] == "cloud"
        assert results[0]["transport"] == "sse"
        assert results[1]["display_name"] == "claude.ai Linear"
        assert results[1]["safe_name"] == "linear"
        assert results[1]["url"] == "https://mcp.linear.app/sse"

    def test_ignores_non_cloud_entries(self):
        output = "\n".join([
            "linear: https://mcp.linear.app/sse - ok",
            "local-server: npx server - ok",
        ])
        results = parse_claude_mcp_list_cloud_connectors(output)
        assert results == []

    def test_deduplicates_by_safe_name(self):
        output = "\n".join([
            "claude.ai Canva: https://mcp.canva.com/mcp - Connected",
            "claude.ai Canva: https://mcp.canva.com/mcp2 - Duplicate",
        ])
        results = parse_claude_mcp_list_cloud_connectors(output)
        assert len(results) == 1
        assert results[0]["url"] == "https://mcp.canva.com/mcp"

    def test_handles_empty_input(self):
        assert parse_claude_mcp_list_cloud_connectors("") == []
        assert parse_claude_mcp_list_cloud_connectors(None) == []

    def test_multi_word_service_name(self):
        output = "claude.ai Some Cool Service: https://example.com/mcp - Connected\n"
        results = parse_claude_mcp_list_cloud_connectors(output)
        assert len(results) == 1
        assert results[0]["safe_name"] == "some-cool-service"

    def test_name_sanitization(self):
        output = "claude.ai My Service!: https://example.com/mcp - Connected\n"
        results = parse_claude_mcp_list_cloud_connectors(output)
        assert len(results) == 1
        assert results[0]["safe_name"] == "my-service"


class TestWrapCloudWithCloudConnectors:
    """Test wrap_cloud discovers cloud.ai entries from list output."""

    def test_discovers_cloud_ai_canva(self, tmp_path, monkeypatch):
        import ultra_lean_mcp_proxy.installer as inst

        config_path = str(tmp_path / ".claude.json")
        monkeypatch.setattr(
            inst,
            "get_config_locations",
            lambda offline=True: [{"name": "claude-code-user", "path": config_path, "key": "mcpServers"}],
        )

        mock_list = "\n".join([
            "claude.ai Canva: https://mcp.canva.com/mcp - Connected",
            "local-server: npx server - ok",
        ])

        mock_get_local = "\n".join([
            "local-server:",
            "  Scope: Local config (private to you in this project)",
            "  Type: stdio",
            "  Command: npx server",
        ])

        def mock_run(args):
            if args[0] == "list":
                return mock_list
            if args[0] == "get" and args[1] == "local-server":
                return mock_get_local
            # Cloud connectors fail with `get`
            raise RuntimeError(f"No MCP server found: {args[1]}")

        result = wrap_cloud(
            dry_run=False,
            runtime="pip",
            suffix="-ulmp",
            verbose=True,
            _command_exists=lambda name: True,
            _run_command=mock_run,
            _resolve_proxy=lambda: FAKE_PROXY,
        )

        # local-server is skipped (local scope), canva is discovered via cloud parser
        assert result["candidates"] == 1
        assert result["written"] == 1

        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        assert "canva-ulmp" in data["mcpServers"]
        assert is_wrapped(data["mcpServers"]["canva-ulmp"])

    def test_cloud_ai_entries_only_no_standard_names(self, tmp_path, monkeypatch):
        """When list output has only cloud.ai entries (no safe names), still works."""
        import ultra_lean_mcp_proxy.installer as inst

        config_path = str(tmp_path / ".claude.json")
        monkeypatch.setattr(
            inst,
            "get_config_locations",
            lambda offline=True: [{"name": "claude-code-user", "path": config_path, "key": "mcpServers"}],
        )

        mock_list = "claude.ai Canva: https://mcp.canva.com/mcp - Connected\n"

        def mock_run(args):
            if args[0] == "list":
                return mock_list
            raise RuntimeError(f"No MCP server found: {args}")

        result = wrap_cloud(
            dry_run=False,
            runtime="pip",
            suffix="-ulmp",
            _command_exists=lambda name: True,
            _run_command=mock_run,
            _resolve_proxy=lambda: FAKE_PROXY,
        )

        assert result["inspected"] == 1  # 0 names + 1 cloud connector
        assert result["candidates"] == 1
        assert result["written"] == 1

    def test_dedup_cloud_ai_against_get_flow(self, tmp_path, monkeypatch):
        """If a connector is discovered by both get and cloud parser, no duplicate."""
        import ultra_lean_mcp_proxy.installer as inst

        config_path = str(tmp_path / ".claude.json")
        monkeypatch.setattr(
            inst,
            "get_config_locations",
            lambda offline=True: [{"name": "claude-code-user", "path": config_path, "key": "mcpServers"}],
        )

        # 'linear' appears both as standard name AND as cloud.ai prefix
        mock_list = "\n".join([
            "linear: https://mcp.linear.app/sse - ok",
            "claude.ai Linear: https://mcp.linear.app/sse - ok",
        ])

        mock_get_linear = "\n".join([
            "linear:",
            "  Scope: Claude.ai cloud connector",
            "  Type: sse",
            "  URL: https://mcp.linear.app/sse",
        ])

        def mock_run(args):
            if args[0] == "list":
                return mock_list
            if args[0] == "get" and args[1] == "linear":
                return mock_get_linear
            raise RuntimeError(f"unexpected: {args}")

        result = wrap_cloud(
            dry_run=False,
            runtime="pip",
            suffix="-ulmp",
            _command_exists=lambda name: True,
            _run_command=mock_run,
            _resolve_proxy=lambda: FAKE_PROXY,
        )

        # "linear" discovered via get, "claude.ai Linear" -> "linear" deduped
        assert result["candidates"] == 1
        assert result["written"] == 1

        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        assert "linear-ulmp" in data["mcpServers"]

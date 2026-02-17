# Ultra Lean MCP Proxy Usage Guide

## Install

```bash
pip install ultra-lean-mcp-proxy
```

## One-Line Install / Uninstall

The `install` command auto-discovers local MCP client configs (Claude Desktop, Cursor, Windsurf, Claude Code), wraps stdio and URL (`http`/`sse`) entries by default, and backs up originals.

### Python
```bash
ultra-lean-mcp-proxy install
```

### Node.js (npx - zero Python dependency)
```bash
npx ultra-lean-mcp-proxy install
```

### Common Options
```bash
# Dry run (preview changes without applying)
ultra-lean-mcp-proxy install --dry-run

# Target a specific client only
ultra-lean-mcp-proxy install --client claude-desktop

# Skip a specific server name in each config
ultra-lean-mcp-proxy install --skip memory

# Opt out of URL/SSE/HTTP wrapping
ultra-lean-mcp-proxy install --no-wrap-url

# Skip cloud connector discovery
ultra-lean-mcp-proxy install --no-cloud

# Customize suffix for cloud-mirrored entries (default: -ulmp)
ultra-lean-mcp-proxy install --suffix -proxy

# Verbose output
ultra-lean-mcp-proxy install -v
```

### Cloud Auto-Discovery

The `install` command automatically discovers cloud-scoped Claude MCP connectors (when the `claude` CLI is on PATH) and mirrors them as wrapped local entries. This runs after the local config wrapping step.

- Enabled by default; opt out with `--no-cloud`
- Use `--suffix` to customize the mirrored entry name suffix (default: `-ulmp`)
- If `claude` is not found on PATH, cloud discovery is silently skipped

### Uninstall
```bash
# Restore original configs
ultra-lean-mcp-proxy uninstall

# Uninstall for a specific client
ultra-lean-mcp-proxy uninstall --client claude-desktop

# Uninstall entries for a specific runtime marker
ultra-lean-mcp-proxy uninstall --runtime npm

# Uninstall all runtimes
ultra-lean-mcp-proxy uninstall --all
```

### Status
```bash
# Check which servers are wrapped and which are not
ultra-lean-mcp-proxy status
```

### Cloud Connectors (Claude, npm CLI)
```bash
# Mirror cloud-scoped Claude URL connectors into local wrapped entries
npx ultra-lean-mcp-proxy wrap-cloud

# Preview only
npx ultra-lean-mcp-proxy wrap-cloud --dry-run -v
```

## Watch Mode (Auto-Update)

The `watch` command monitors MCP config files and automatically wraps new stdio and URL entries as they are added.

```bash
# Watch config files, auto-wrap new servers
ultra-lean-mcp-proxy watch

# Opt out of URL/SSE/HTTP wrapping in watch mode
ultra-lean-mcp-proxy watch --no-wrap-url

# Run as background daemon
ultra-lean-mcp-proxy watch --daemon

# Stop daemon
ultra-lean-mcp-proxy watch --stop
```

### Cloud Auto-Discovery

When `claude` CLI is available on PATH, watch mode automatically discovers cloud-scoped Claude MCP connectors and mirrors them as wrapped local entries. This runs periodically alongside the file-watching loop.

```bash
# Set cloud discovery interval (default: 60 seconds)
ultra-lean-mcp-proxy watch --cloud-interval 30

# Customize suffix for mirrored entries (default: -ulmp)
ultra-lean-mcp-proxy watch --suffix -proxy

# Combine with other watch options
ultra-lean-mcp-proxy watch --daemon --cloud-interval 120 --suffix -ulmp -v
```

If `claude` is not found on PATH, cloud discovery is silently disabled and the watcher operates normally.

## Run as Proxy

```bash
ultra-lean-mcp-proxy proxy -- npx -y @modelcontextprotocol/server-filesystem .
```

By default all v2 vectors are enabled. Example of explicit tuning:

```bash
ultra-lean-mcp-proxy proxy \
  --lazy-mode minimal \
  --tools-hash-refresh-interval 50 \
  --cache-ttl 180 \
  -- npx -y @modelcontextprotocol/server-filesystem .
```

## Config and Env

- Config file: `ultra-lean-mcp-proxy.config.json` or `.yaml`
- Env prefix: `ULTRA_LEAN_MCP_PROXY_`

## Extension Namespace

- Capability: `capabilities.experimental.ultra_lean_mcp_proxy`
- Per-request params: `_ultra_lean_mcp_proxy`
- Meta tool: `ultra_lean_mcp_proxy.search_tools`

## Notes

- Compile/decompile utilities moved to `ultra-lean-mcp` and `ultra-lean-mcp-core`.
- Historical benchmark result files retain legacy naming for reproducibility.

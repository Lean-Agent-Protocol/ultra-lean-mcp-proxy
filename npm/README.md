<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/lean-agent-protocol/ultra-lean-mcp-proxy/main/.github/logo-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/lean-agent-protocol/ultra-lean-mcp-proxy/main/.github/logo-light.png">
  <img alt="LAP Logo" src="https://raw.githubusercontent.com/lean-agent-protocol/ultra-lean-mcp-proxy/main/.github/logo-dark.png" width="400">
</picture>

# ultra-lean-mcp-proxy

[![PyPI](https://img.shields.io/pypi/v/ultra-lean-mcp-proxy?color=blue)](https://pypi.org/project/ultra-lean-mcp-proxy/)
[![npm](https://img.shields.io/npm/v/ultra-lean-mcp-proxy?color=red)](https://www.npmjs.com/package/ultra-lean-mcp-proxy)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Transparent MCP stdio proxy that reduces token and byte overhead on `tools/list` and `tools/call` paths using LAP (Lean Agent Protocol) compression.

## One-Line Install

### Python (pip)
```bash
pip install ultra-lean-mcp-proxy
ultra-lean-mcp-proxy install
```

### Node.js (npx - zero Python dependency)
```bash
npx ultra-lean-mcp-proxy install
```

Both commands auto-discover local MCP client configs (Claude Desktop, Cursor, Windsurf, Claude Code), wrap stdio and URL (`http`/`sse`) entries by default, and back up originals.

To uninstall:
```bash
ultra-lean-mcp-proxy uninstall
```

To check current status:
```bash
ultra-lean-mcp-proxy status
```

## Add Servers That Get Wrapped

`ultra-lean-mcp-proxy install` wraps local stdio servers (`command` + `args`) and local URL-based transports (`http` / `sse`) by default.
Use `--no-wrap-url` if you only want stdio wrapping.

For Claude Code, add servers in stdio form and use `--scope user` so they are written to `~/.claude.json` (auto-detected):

```bash
# Wrappable (stdio)
claude mcp add --scope user filesystem -- npx -y @modelcontextprotocol/server-filesystem /tmp
```

```bash
# Wrappable by default (wrapped via local bridge chain)
claude mcp add --scope user --transport http linear https://mcp.linear.app/mcp
```

Then run:

```bash
ultra-lean-mcp-proxy status
ultra-lean-mcp-proxy install
```

> **Note**: `claude mcp add --scope project ...` writes to `.mcp.json` in the current project. This file is not globally auto-discovered by `install` yet.

> **Note**: URL wrapping applies to local config files (for example `~/.claude.json`, `~/.cursor/mcp.json`).  
> For cloud-managed Claude connectors, use npm CLI `wrap-cloud` to mirror and wrap them locally:
> `npx ultra-lean-mcp-proxy wrap-cloud`

## Features

- **Transparent Proxying**: Wrap any MCP stdio server without code changes
- **Massive Token Savings**: 51-83% token reduction across real MCP servers
- **Performance Boost**: 22-87% faster response times
- **Zero Client Changes**: Compatible with existing MCP clients
- **Tools Hash Sync**: Efficient tool list caching with conditional requests
- **Delta Responses**: Send only changes between responses
- **Lazy Loading**: On-demand tool discovery for large tool sets
- **Result Compression**: Compress tool call results using LAP format

## Performance Benchmarks

Benchmark figures below are for the Python runtime with the full v2 optimization pipeline enabled.
The npm package in Phase C1 currently provides definition compression only.

Real-world benchmark across 5 production MCP servers (147 measured turns):

| Metric | Direct | With Proxy | Savings |
|--------|--------|------------|---------|
| **Total Tokens** | 82,631 | 23,826 | **71.2%** |
| **Response Time** | 1,047ms | 540ms | **48.4%** |

### Per-Server Results

| Server | Token Savings | Time Savings | Tools |
|--------|---------------|--------------|-------|
| **filesystem** | 72.4% | 87.3% | list_directory, search_files |
| **memory** | 82.7% | 31.8% | read_graph, search_nodes |
| **everything** | 65.2% | 22.1% | get-resource-links, research |
| **sequential-thinking** | 61.5% | 3.8% | sequentialthinking |
| **puppeteer** | 51.2% | -9.7% | puppeteer_navigate, evaluate |

*Note: Puppeteer showed time overhead due to heavy I/O operations, but still achieved 51% token savings.*

## Installation

### Basic Installation
```bash
pip install ultra-lean-mcp-proxy
```

### With Proxy Support (Recommended)
```bash
pip install 'ultra-lean-mcp-proxy[proxy]'
```

### Development Installation
```bash
pip install 'ultra-lean-mcp-proxy[dev]'
```

## Quick Start

### Wrap Any MCP Server

```bash
# Wrap the filesystem server
ultra-lean-mcp-proxy proxy -- npx -y @modelcontextprotocol/server-filesystem /tmp

# Wrap a Python MCP server
ultra-lean-mcp-proxy proxy -- python -m my_mcp_server

# Wrap with runtime stats
ultra-lean-mcp-proxy proxy --stats -- npx -y @modelcontextprotocol/server-memory

# Enable verbose logging
ultra-lean-mcp-proxy proxy -v -- npx -y @modelcontextprotocol/server-everything
```

### Claude Desktop Integration

Update your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "filesystem-optimized": {
      "command": "ultra-lean-mcp-proxy",
      "args": [
        "proxy",
        "--stats",
        "--",
        "npx",
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/Users/yourname/Documents"
      ]
    }
  }
}
```

Now when Claude uses the filesystem server, all communication is automatically optimized.

## Configuration

### Command-Line Flags

```bash
# All optimization vectors are ON by default.
# Use --disable-* flags to opt out.
ultra-lean-mcp-proxy proxy \
  --disable-lazy-loading \
  -- <upstream-command>

# Fine-tune optimization parameters
ultra-lean-mcp-proxy proxy \
  --result-compression-mode aggressive \
  --lazy-mode search_only \
  --cache-ttl 3600 \
  --delta-min-savings 0.15 \
  -- <upstream-command>

# Dump effective configuration
ultra-lean-mcp-proxy proxy --dump-effective-config -- <upstream-command>
```

### Configuration File

Create `ultra-lean-mcp-proxy.config.json` or `.yaml`:

```json
{
  "result_compression_enabled": true,
  "result_compression_mode": "aggressive",
  "delta_responses_enabled": true,
  "lazy_loading_enabled": true,
  "lazy_mode": "search_only",
  "tools_hash_sync_enabled": true,
  "caching_enabled": true,
  "cache_ttl_seconds": 3600
}
```

Load with:
```bash
ultra-lean-mcp-proxy proxy --config ultra-lean-mcp-proxy.config.json -- <upstream-command>
```

### Environment Variables

Prefix any config option with `ULTRA_LEAN_MCP_PROXY_`:

```bash
export ULTRA_LEAN_MCP_PROXY_RESULT_COMPRESSION_ENABLED=true
export ULTRA_LEAN_MCP_PROXY_CACHE_TTL_SECONDS=3600
ultra-lean-mcp-proxy proxy -- <upstream-command>
```

## Optimization Features

### 1. Tool Definition Compression

Compresses `tools/list` responses using LAP format:

**Before (JSON Schema):**
```json
{
  "name": "search_files",
  "description": "Search for files matching a pattern",
  "inputSchema": {
    "type": "object",
    "properties": {
      "pattern": {"type": "string", "description": "Glob pattern"},
      "max_results": {"type": "number", "default": 100}
    },
    "required": ["pattern"]
  }
}
```

**After (LAP):**
```
@tool search_files
@desc Search for files matching a pattern
@in pattern:string Glob pattern
@opt max_results:number=100
```

### 2. Tools Hash Sync

Efficient caching using conditional requests:
- Client: "Give me tools if hash != abc123"
- Server (unchanged): `304 Not Modified`
- Server (changed): `200 OK` with new tools

Hit ratio in benchmarks: **84.1%** (37 hits, 7 misses)

### 3. Delta Responses

Send only changes between tool calls:

**First call:**
```json
{"status": "running", "progress": 0, "message": "Starting..."}
```

**Second call (delta):**
```json
{"progress": 50, "message": "Processing..."}
```

### 4. Lazy Loading

Load tools on-demand instead of all at once:

- **Off**: All tools sent upfront
- **Minimal**: Send 5 most-used tools initially
- **Search Only**: Only send search/discovery tools, load others when called

Best for servers with 20+ tools.

### 5. Result Compression

Compress tool call results:

- **Balanced**: Compress descriptions, preserve structure
- **Aggressive**: Maximum compression, lean LAP format

## CLI Reference

### Install / Uninstall
```bash
# Install: wrap all MCP servers with proxy
ultra-lean-mcp-proxy install [--dry-run] [--client NAME] [--skip NAME] [--offline] [--no-wrap-url] [--no-cloud] [--suffix NAME] [-v]
# `--skip` matches MCP server names inside config files

# Uninstall: restore original configs
ultra-lean-mcp-proxy uninstall [--dry-run] [--client NAME] [--runtime pip|npm] [--all] [-v]

# Check status
ultra-lean-mcp-proxy status

# Mirror cloud-scoped Claude URL connectors into local wrapped entries (npm CLI)
npx ultra-lean-mcp-proxy wrap-cloud [--dry-run] [--runtime npm|pip] [--suffix -ulmp] [-v]
```

### Watch Mode (Auto-Update)
```bash
# Watch config files, auto-wrap new servers
ultra-lean-mcp-proxy watch

# Watch but keep URL/SSE/HTTP entries unwrapped
ultra-lean-mcp-proxy watch --no-wrap-url

# Run as background daemon
ultra-lean-mcp-proxy watch --daemon

# Stop daemon
ultra-lean-mcp-proxy watch --stop

# Set cloud connector discovery interval (default: 60s)
ultra-lean-mcp-proxy watch --cloud-interval 30

# Customize suffix for cloud-mirrored entries
ultra-lean-mcp-proxy watch --suffix -proxy
```

Watch mode auto-discovers cloud-scoped Claude MCP connectors when the `claude` CLI is available on PATH, polling every `--cloud-interval` seconds.

### Proxy (Direct Usage)
```bash
ultra-lean-mcp-proxy proxy [--enable-<feature>|--disable-<feature>] [--cache-ttl SEC] [--lazy-mode MODE] -- <upstream-command> [args...]
```

For troubleshooting, you can enable per-server RPC tracing:

```bash
ultra-lean-mcp-proxy proxy --trace-rpc -- <upstream-command>
```

## Architecture

```
┌──────────┐           ┌────────────────────┐           ┌──────────┐
│          │  stdio    │  ultra-lean-mcp    │  stdio    │ Upstream │
│  Client  │◄─────────►│      proxy         │◄─────────►│   MCP    │
│ (Claude) │           │                    │           │  Server  │
│          │  LAP      │  ┌──────────────┐  │  JSON     │          │
└──────────┘           │  │ Compression  │  │           └──────────┘
                       │  │ Delta Engine │  │
                       │  │ Cache Layer  │  │
                       │  │ Lazy Loader  │  │
                       │  └──────────────┘  │
                       └────────────────────┘
```

The proxy:
1. Sits between client and server as transparent stdio relay
2. Intercepts `tools/list` and `tools/call` JSON-RPC messages
3. Compresses outgoing responses using LAP format
4. Decompresses incoming requests back to JSON Schema
5. Maintains delta state, cache, and tool registry

## Use Cases

### Production MCP Servers
Wrap existing MCP servers to reduce LLM token costs and improve response times.

### High-Volume Tool Servers
Servers with 50+ tools benefit from lazy loading and tools hash sync.

### Low-Bandwidth Environments
Reduce network payload sizes by 50-70% with compression.

### Development & Testing
Run with `--stats` to understand token usage patterns and optimization effectiveness.

## Monitoring & Stats

Enable stats logging:
```bash
ultra-lean-mcp-proxy proxy --stats -- <upstream-command>
```

Output to stderr:
```
[2026-02-15 10:28:55] Token savings: 71.2% (82631 → 23826)
[2026-02-15 10:28:55] Time savings: 48.4% (1047ms → 540ms)
[2026-02-15 10:28:55] tools_hash hit ratio: 37:7 (84.1% hits)
[2026-02-15 10:28:55] Upstream traffic: 2858 req tokens, 22528 resp tokens
```

## Related Projects

- **[ultra-lean-mcp-core](https://github.com/lean-agent-protocol/ultra-lean-mcp-core)** - Zero-dependency core library for LAP compilation/decompilation
- **[ultra-lean-mcp](https://github.com/lean-agent-protocol/ultra-lean-mcp)** - MCP server + CLI for LAP workflows

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT License - see [LICENSE](LICENSE) for details.

---

Part of the [Lean Agent Protocol](https://github.com/lean-agent-protocol) ecosystem.

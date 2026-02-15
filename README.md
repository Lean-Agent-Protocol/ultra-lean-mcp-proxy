<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/lean-agent-protocol/ultra-lean-mcp-proxy/main/.github/logo-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/lean-agent-protocol/ultra-lean-mcp-proxy/main/.github/logo-light.png">
  <img alt="LAP Logo" src="https://raw.githubusercontent.com/lean-agent-protocol/ultra-lean-mcp-proxy/main/.github/logo-dark.png" width="400">
</picture>

# ultra-lean-mcp-proxy

[![PyPI](https://img.shields.io/pypi/v/ultra-lean-mcp-proxy?color=blue)](https://pypi.org/project/ultra-lean-mcp-proxy/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Transparent MCP stdio proxy that reduces token and byte overhead on `tools/list` and `tools/call` paths using LAP (Lean Agent Protocol) compression.

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
# Enable/disable optimization features
ultra-lean-mcp-proxy proxy \
  --enable-result-compression \
  --enable-delta-responses \
  --enable-lazy-loading \
  --enable-tools-hash-sync \
  --enable-caching \
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

## Benchmarks

See `benchmarks/` directory for:
- `results_v2_live_servers.md` - Real server benchmark results
- `results_v2_real_world.md` - Production workload analysis
- `ACCURACY_RESULTS.md` - Roundtrip validation results

Run benchmarks:
```bash
cd benchmarks
python benchmark_live_servers.py
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT License - see [LICENSE](LICENSE) for details.

---

Part of the [Lean Agent Protocol](https://github.com/lean-agent-protocol) ecosystem.

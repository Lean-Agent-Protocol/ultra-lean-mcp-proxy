# Ultra Lean MCP Proxy v2 API

## CLI

```bash
ultra-lean-mcp-proxy proxy [flags] -- <upstream command>
```

## Core Flags

- `--enable-result-compression` / `--disable-result-compression`
- `--enable-delta-responses` / `--disable-delta-responses`
- `--enable-lazy-loading` / `--disable-lazy-loading`
- `--enable-tools-hash-sync` / `--disable-tools-hash-sync`
- `--enable-caching` / `--disable-caching`
- `--lazy-mode off|minimal|search_only`
- `--cache-ttl <seconds>`

## Env Variables

All env controls use `ULTRA_LEAN_MCP_PROXY_` prefix, e.g.:

- `ULTRA_LEAN_MCP_PROXY_CONFIG`
- `ULTRA_LEAN_MCP_PROXY_VERBOSE`
- `ULTRA_LEAN_MCP_PROXY_STATS`
- `ULTRA_LEAN_MCP_PROXY_RESULT_COMPRESSION`
- `ULTRA_LEAN_MCP_PROXY_DELTA_RESPONSES`
- `ULTRA_LEAN_MCP_PROXY_LAZY_LOADING`
- `ULTRA_LEAN_MCP_PROXY_TOOLS_HASH_SYNC`
- `ULTRA_LEAN_MCP_PROXY_CACHING`

## Protocol Extensions

Initialize capability advertisement:

- `capabilities.experimental.ultra_lean_mcp_proxy.tools_hash_sync.version = 1`

Conditional tools/list request extension:

- `params._ultra_lean_mcp_proxy.tools_hash_sync.if_none_match = "sha256:<hash>"`

Proxy response extension envelope:

- `result._ultra_lean_mcp_proxy.tools_hash_sync`
- `result._ultra_lean_mcp_proxy.result_compression`
- `result._ultra_lean_mcp_proxy.runtime_metrics` (when stats enabled)

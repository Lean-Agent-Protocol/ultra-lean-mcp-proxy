# Ultra Lean MCP Proxy Usage Guide

## Install

```bash
pip install ultra-lean-mcp-proxy
```

## Run as Proxy

```bash
ultra-lean-mcp-proxy proxy -- npx -y @modelcontextprotocol/server-filesystem .
```

With all v2 vectors enabled:

```bash
ultra-lean-mcp-proxy proxy \
  --enable-result-compression \
  --enable-delta-responses \
  --enable-lazy-loading \
  --lazy-mode minimal \
  --enable-tools-hash-sync \
  --tools-hash-refresh-interval 50 \
  --enable-caching \
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

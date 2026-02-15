# Ultra Lean MCP Proxy v2 Implementation Plan

## Delivered Scope in This Repo

- proxy package rename to `ultra_lean_mcp_proxy`
- proxy-only CLI command (`ultra-lean-mcp-proxy`)
- protocol extension namespace migration to `_ultra_lean_mcp_proxy`
- capability namespace migration to `experimental.ultra_lean_mcp_proxy`
- env prefix migration to `ULTRA_LEAN_MCP_PROXY_`
- shared compression utilities consumed from `ultra-lean-mcp-core`

## Split Model

- `ultra-lean-mcp-core`: shared LAP/compiler/compression library
- `ultra-lean-mcp`: upstream MCP server + utility CLI
- `ultra-lean-mcp-proxy`: runtime optimization proxy

## Validation

- proxy tests pass in this repo
- core tests pass in sibling core repo
- server tests pass in sibling server repo

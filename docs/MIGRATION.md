# Migration: LeanMCP -> Ultra Lean MCP Proxy

This repository is now **Ultra Lean MCP Proxy** and is a hard cutover.

## Breaking Changes

- Python package renamed: `leanmcp` -> `ultra_lean_mcp_proxy`
- CLI renamed: `leanmcp` -> `ultra-lean-mcp-proxy`
- Protocol extension key renamed: `_leanmcp` -> `_ultra_lean_mcp_proxy`
- Capability namespace renamed: `capabilities.experimental.leanmcp` -> `capabilities.experimental.ultra_lean_mcp_proxy`
- Meta-tool renamed: `leanmcp.search_tools` -> `ultra_lean_mcp_proxy.search_tools`
- Env var prefix renamed: `LEANMCP_` -> `ULTRA_LEAN_MCP_PROXY_`

## Moved Functionality

Compile/decompile/compression core APIs moved out of this repo:

- `ultra-lean-mcp-core` (shared library)
- `ultra-lean-mcp` (upstream MCP server + utility CLI)

No compatibility aliases are kept in this repository.

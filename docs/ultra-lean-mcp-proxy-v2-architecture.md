# Ultra Lean MCP Proxy v2 Architecture

## Purpose

Ultra Lean MCP Proxy is a transparent optimization layer between any MCP client and any stdio MCP server.

## Pipelines

- `tools/list`: definition compression, lazy visibility modes, tools hash sync
- `tools/call`: cache lookup, result compression, delta envelopes, cache writeback
- passthrough for other methods

## Major Components

- transport bridge (stdio JSON-RPC)
- method-aware interceptors
- in-memory state store (`ProxyState`)
- result compression engine
- delta engine
- tools hash sync engine

## Compatibility

- non-extension clients remain fail-open compatible
- extension behavior activates only after capability negotiation

## Shared Dependencies

Shared schema/description compaction helpers are consumed from `ultra-lean-mcp-core`.

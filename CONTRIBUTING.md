# Contributing to ultra-lean-mcp-proxy

Thank you for your interest in contributing to ultra-lean-mcp-proxy! This document provides guidelines and instructions for contributing.

## Development Setup

### Prerequisites

- Python 3.10 or higher
- Git
- Node.js (for testing with upstream MCP servers)

### Installation

1. Clone the repository:
```bash
git clone https://github.com/lean-agent-protocol/ultra-lean-mcp-proxy.git
cd ultra-lean-mcp-proxy
```

2. Install in development mode:
```bash
pip install -e '.[dev,proxy]'
```

This installs:
- The package in editable mode
- Development dependencies (pytest, tiktoken)
- MCP proxy dependencies

### npm Package Development

The npm package lives in the `npm/` directory and has zero dependencies.

```bash
# Syntax check all files
node --check npm/bin/cli.mjs
node --check npm/src/compress.mjs
node --check npm/src/installer.mjs
node --check npm/src/proxy.mjs
node --check npm/src/watcher.mjs

# Run Node tests
node --test "npm/test/*.test.mjs"

# Test the CLI
node npm/bin/cli.mjs status
node npm/bin/cli.mjs install --dry-run

# Test the proxy
node npm/bin/cli.mjs proxy -- npx -y @modelcontextprotocol/server-filesystem /tmp
```

### Project Structure

```
ultra-lean-mcp-proxy/
├── src/
│   └── ultra_lean_mcp_proxy/
│       ├── __init__.py
│       ├── cli.py                # CLI entry point (install/uninstall/status/proxy/watch)
│       ├── installer.py          # Config discovery, wrap/unwrap, backup
│       ├── watcher.py            # File watch + auto-wrap
│       ├── proxy.py              # Main proxy runtime (full features)
│       ├── config.py             # Configuration management
│       ├── delta.py              # Delta response engine
│       ├── result_compression.py # Result compression
│       ├── state.py              # Proxy state management
│       └── tools_hash_sync.py    # Tools hash sync
├── npm/
│   ├── package.json
│   ├── bin/
│   │   └── cli.mjs              # Node.js CLI entry point
│   └── src/
│       ├── installer.mjs        # Config discovery + wrap/unwrap
│       ├── proxy.mjs            # Basic stdio proxy (definition compression)
│       ├── compress.mjs         # Description/schema compression
│       └── watcher.mjs          # File watch + auto-wrap
├── registry/
│   └── clients.json             # Remote client registry
├── tests/
│   ├── test_installer.py
│   └── ...
├── benchmarks/
├── README.md
├── pyproject.toml
└── LICENSE
```

## Code Style Guidelines

### Python Style

- Follow [PEP 8](https://peps.python.org/pep-0008/) for code style
- Use type hints for all function signatures
- Use async/await for I/O operations
- Maximum line length: 120 characters
- Use descriptive variable names

### Proxy Architecture Guidelines

- **Transparency**: Proxy should be invisible to clients and servers
- **Stateless Operations**: Avoid persisting state across sessions when possible
- **Error Handling**: Always gracefully handle upstream server errors
- **Performance**: Minimize overhead, measure token/time savings

### Example

```python
async def intercept_tools_list(
    response: dict[str, Any],
    config: ProxyConfig
) -> dict[str, Any]:
    """
    Intercept and compress tools/list response.

    Args:
        response: Original JSON-RPC response from upstream
        config: Proxy configuration

    Returns:
        Modified response with compressed tool definitions
    """
    if not config.definition_compression_enabled:
        return response

    # Compress tools...
    return compressed_response
```

## Testing Requirements

### Running Tests

Run all tests:
```bash
PYTHONPATH=src python -m pytest -q
```

Run with coverage:
```bash
PYTHONPATH=src python -m pytest --cov=ultra_lean_mcp_proxy --cov-report=term-missing
```

### Integration Testing

Test with a real MCP server:

```bash
# Start proxy wrapping filesystem server
ultra-lean-mcp-proxy proxy -v -- \
  npx -y @modelcontextprotocol/server-filesystem /tmp

# In another terminal, use MCP Inspector to test
mcp-inspector ultra-lean-mcp-proxy proxy -- \
  npx -y @modelcontextprotocol/server-filesystem /tmp
```

### Benchmark Testing

Run live server benchmarks:

```bash
cd benchmarks
python benchmark_live_servers.py

# View results
cat results_v2_live_servers.md
```

Benchmarks test:
- Token savings across real MCP servers
- Response time improvements
- Cache hit ratios
- Delta compression effectiveness

### Testing Configuration

Test different config combinations:

```bash
# Test with all optimizations disabled
ultra-lean-mcp-proxy proxy \
  --disable-result-compression \
  --disable-delta-responses \
  --disable-lazy-loading \
  -- <server>

# Test aggressive mode
ultra-lean-mcp-proxy proxy \
  --result-compression-mode aggressive \
  --enable-lazy-loading \
  --lazy-mode search_only \
  -- <server>
```

## Development Workflow

### Adding a New Optimization Feature

1. **Design**: Document the optimization strategy
2. **Implement**: Add feature with config toggle
3. **Test**: Write unit and integration tests
4. **Benchmark**: Measure token/time impact
5. **Document**: Update README with feature description

Example workflow:
```bash
# 1. Create feature branch
git checkout -b feature/new-optimization

# 2. Implement with tests
# ... code changes ...

# 3. Run benchmarks
cd benchmarks
python benchmark_live_servers.py

# 4. Compare results
diff results_v2_live_servers.md results_v2_live_servers.md.backup

# 5. Update docs
# ... README changes ...

# 6. Commit and PR
git commit -am "Add new optimization feature"
```

### Debugging the Proxy

Enable verbose logging:
```bash
ultra-lean-mcp-proxy proxy -v -- <server>
```

Output shows:
- JSON-RPC messages (request/response)
- Compression decisions
- Cache hits/misses
- Delta calculations
- Token savings

### Testing with Claude Desktop

1. Update config:
```json
{
  "mcpServers": {
    "test-proxy": {
      "command": "python",
      "args": [
        "-m",
        "ultra_lean_mcp_proxy.cli",
        "proxy",
        "--stats",
        "-v",
        "--",
        "npx",
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/tmp"
      ]
    }
  }
}
```

2. Restart Claude Desktop
3. Monitor proxy logs in stderr
4. Verify optimizations are working

## Pull Request Process

### Before Submitting

1. **Run Tests**: Ensure all tests pass
   ```bash
   PYTHONPATH=src python -m pytest -q
   ```

2. **Run Benchmarks**: Verify performance impact
   ```bash
   cd benchmarks
   python benchmark_live_servers.py
   ```

3. **Test Integration**: Test with real MCP servers
   ```bash
   ultra-lean-mcp-proxy proxy -v -- npx -y @modelcontextprotocol/server-filesystem /tmp
   ```

4. **Update Benchmarks**: Document performance changes in PR

### PR Guidelines

1. **Fork** the repository
2. **Create a branch**: `git checkout -b feature/my-feature`
3. **Make changes** following guidelines
4. **Write tests** for new functionality
5. **Run benchmarks** to measure impact
6. **Update documentation**
7. **Commit** with clear messages
8. **Push** to your fork
9. **Open a PR** with benchmark results

### PR Description Template

```markdown
## Summary
Brief description of changes

## Motivation
Why this change is needed

## Performance Impact
- Token savings: X% → Y%
- Time savings: A% → B%
- Tested with: [list of MCP servers]

## Changes
- Changed X to Y
- Added Z feature
- Updated documentation

## Testing
- [ ] Unit tests pass
- [ ] Integration tests pass
- [ ] Benchmarks updated
- [ ] Manual testing completed

## Breaking Changes
List any breaking changes (or "None")
```

## Code Review

Reviewers will check:

- Code quality and async patterns
- Test coverage (unit + integration)
- Performance impact (benchmarks)
- Configuration handling
- Error handling and graceful degradation
- Documentation updates
- Backward compatibility

## Benchmark Guidelines

### Writing Benchmarks

Benchmarks should:
- Test real-world scenarios
- Use actual MCP servers when possible
- Measure both tokens and time
- Report statistical significance
- Include warmup phase to prime caches

### Benchmark Metrics

Track:
- **Token Savings**: `(original - compressed) / original * 100`
- **Time Savings**: `(direct - proxy) / direct * 100`
- **Cache Hit Ratio**: `hits / (hits + misses)`
- **Delta Effectiveness**: Percentage of responses using deltas
- **Overhead**: Proxy processing time

### Updating Benchmarks

When making changes:
1. Run baseline: `python benchmark_live_servers.py > baseline.md`
2. Make changes
3. Run new benchmark: `python benchmark_live_servers.py > updated.md`
4. Compare: `diff baseline.md updated.md`
5. Document in PR

## Dependencies

This package depends on:
- **ultra-lean-mcp-core** (required) - Core LAP functionality
- **mcp** (optional) - MCP SDK for proxy functionality
- **tiktoken** (dev) - Token counting for benchmarks

When adding features:
- Minimize new dependencies
- Make dependencies optional when possible
- Document dependency rationale

## Performance Considerations

When contributing:
- **Measure First**: Always benchmark before optimizing
- **Minimize Overhead**: Keep proxy processing time low
- **Async All The Way**: Use async/await for I/O
- **Graceful Degradation**: Fall back to pass-through on errors
- **Cache Wisely**: Balance memory usage vs. hit rate

## Getting Help

- **Issues**: Open an issue for bugs or feature requests
- **Discussions**: Use GitHub Discussions for design questions
- **Benchmarks**: Check `benchmarks/` for performance data
- **Documentation**: See README and MCP protocol docs

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

---

Thank you for contributing to the Lean Agent Protocol ecosystem!

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { loadProxyConfig } from '../src/config.mjs';

function makeTempConfig(data) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ulmp-node-config-'));
  const file = path.join(dir, 'ultra-lean-mcp-proxy.config.json');
  fs.writeFileSync(file, JSON.stringify(data, null, 2), 'utf-8');
  return { dir, file };
}

test('loadProxyConfig merges file + env + cli overrides with cli precedence', () => {
  const { dir, file } = makeTempConfig({
    optimizations: {
      caching: { enabled: false, default_ttl_seconds: 30 },
      delta_responses: { enabled: false },
      tools_hash_sync: { enabled: false, refresh_interval: 20 },
    },
  });
  try {
    const cfg = loadProxyConfig({
      upstreamCommand: ['node', 'server.mjs'],
      configPath: file,
      env: {
        ULTRA_LEAN_MCP_PROXY_CACHING: '1',
        ULTRA_LEAN_MCP_PROXY_CACHE_TTL_SECONDS: '40',
      },
      cliOverrides: {
        caching: true,
        cacheTtl: 120,
        deltaResponses: true,
        toolsHashSync: true,
        toolsHashRefreshInterval: 4,
      },
    });
    assert.equal(cfg.cachingEnabled, true);
    assert.equal(cfg.cacheTtlSeconds, 120);
    assert.equal(cfg.deltaResponsesEnabled, true);
    assert.equal(cfg.toolsHashSyncEnabled, true);
    assert.equal(cfg.toolsHashSyncRefreshInterval, 4);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('loadProxyConfig sets server profile by command match', () => {
  const { dir, file } = makeTempConfig({
    servers: {
      default: {
        optimizations: { caching: { enabled: false } },
      },
      github: {
        match: { command_contains: 'server-github' },
        optimizations: {
          caching: { enabled: true, default_ttl_seconds: 15 },
          lazy_loading: { enabled: true, mode: 'minimal' },
        },
      },
    },
  });
  try {
    const cfg = loadProxyConfig({
      upstreamCommand: ['npx', '@modelcontextprotocol/server-github'],
      configPath: file,
    });
    assert.equal(cfg.serverName, 'github');
    assert.equal(cfg.cachingEnabled, true);
    assert.equal(cfg.cacheTtlSeconds, 15);
    assert.equal(cfg.lazyMode, 'minimal');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('loadProxyConfig rejects unsupported tools hash algorithm', () => {
  const { dir, file } = makeTempConfig({
    optimizations: {
      tools_hash_sync: { enabled: true, algorithm: 'sha1' },
    },
  });
  try {
    assert.throws(
      () => loadProxyConfig({ upstreamCommand: ['node', 'server.mjs'], configPath: file }),
      /tools hash sync algorithm/i
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});


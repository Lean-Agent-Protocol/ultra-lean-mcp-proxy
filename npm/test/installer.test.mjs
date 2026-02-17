import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  cleanEnvForClaude,
  doInstall,
  doUninstall,
  doWrapCloud,
  getRuntime,
  getWrappedTransport,
  isClaudeCloudScope,
  isClaudeLocalScope,
  isSafePropertyName,
  isWrapped,
  isUrlBridgeAvailable,
  parseClaudeMcpGetDetails,
  parseClaudeMcpListCloudConnectors,
  parseClaudeMcpListNames,
  wrapUrlEntry,
  normalizeRegistryEntries,
  unwrapEntry,
  wrapEntry,
} from '../src/installer.mjs';

function makeTempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'ulmp-node-test-'));
}

function setTempHome(tempHome) {
  const previous = {
    HOME: process.env.HOME,
    USERPROFILE: process.env.USERPROFILE,
  };
  process.env.HOME = tempHome;
  process.env.USERPROFILE = tempHome;
  return () => {
    if (previous.HOME === undefined) delete process.env.HOME;
    else process.env.HOME = previous.HOME;
    if (previous.USERPROFILE === undefined) delete process.env.USERPROFILE;
    else process.env.USERPROFILE = previous.USERPROFILE;
  };
}

function setTempPathWithFakeProxy(tempDir) {
  const binDir = path.join(tempDir, 'bin');
  fs.mkdirSync(binDir, { recursive: true });
  const previousPath = process.env.PATH;
  if (process.platform === 'win32') {
    const cmdPath = path.join(binDir, 'ultra-lean-mcp-proxy.cmd');
    fs.writeFileSync(cmdPath, '@echo off\r\n', 'utf-8');
  } else {
    const exePath = path.join(binDir, 'ultra-lean-mcp-proxy');
    fs.writeFileSync(exePath, '#!/usr/bin/env sh\nexit 0\n', 'utf-8');
    fs.chmodSync(exePath, 0o755);
  }
  process.env.PATH = `${binDir}${path.delimiter}${previousPath || ''}`;
  return () => {
    if (previousPath === undefined) delete process.env.PATH;
    else process.env.PATH = previousPath;
  };
}

test('wrap/unwrap roundtrip preserves original command and args', () => {
  const original = { command: 'npx', args: ['@modelcontextprotocol/server-filesystem', '/tmp'] };
  const wrapped = wrapEntry(original, '/abs/ultra-lean-mcp-proxy', 'npm');
  assert.equal(isWrapped(wrapped), true);
  assert.equal(getRuntime(wrapped), 'npm');
  const restored = unwrapEntry(wrapped);
  assert.deepEqual(restored, original);
});

test('wrapUrlEntry/unwrapEntry roundtrip restores original URL entry', () => {
  const original = {
    url: 'https://mcp.example.com/sse',
    headers: { Authorization: 'Bearer token' },
  };
  const wrapped = wrapUrlEntry(original, '/abs/ultra-lean-mcp-proxy', 'npm');
  assert.equal(isWrapped(wrapped), true);
  assert.equal(getRuntime(wrapped), 'npm');
  assert.equal(getWrappedTransport(wrapped), 'url');
  const restored = unwrapEntry(wrapped);
  assert.deepEqual(restored, original);
});

test('wrapUrlEntry escapes URL metacharacters for Windows cmd bridge', () => {
  const original = {
    url: 'https://mcp.example.com/sse?mode=a&pipe=b|c',
  };
  const wrapped = wrapUrlEntry(original, '/abs/ultra-lean-mcp-proxy', 'npm');
  const dashIdx = wrapped.args.indexOf('--');
  const bridge = wrapped.args.slice(dashIdx + 1);
  if (process.platform === 'win32') {
    assert.deepEqual(bridge.slice(0, 5), ['cmd', '/c', 'npx', '-y', 'mcp-remote']);
    assert.equal(bridge[5], 'https://mcp.example.com/sse?mode=a^&pipe=b^|c');
  } else {
    assert.deepEqual(bridge.slice(0, 3), ['npx', '-y', 'mcp-remote']);
    assert.equal(bridge[3], original.url);
  }
});

test('normalizeRegistryEntries supports versioned registry payload format', () => {
  const payload = {
    version: 1,
    clients: [
      {
        name: 'claude-desktop',
        paths: {
          win32: '%APPDATA%/Claude/claude_desktop_config.json',
          darwin: '~/.config/claude/claude_desktop_config.json',
          linux: '~/.config/claude/claude_desktop_config.json',
        },
        key: 'mcpServers',
      },
    ],
  };

  const entries = normalizeRegistryEntries(payload, { strict: true });
  assert.equal(entries.length, 1);
  assert.equal(entries[0].name, 'claude-desktop');
  assert.equal(entries[0].serverKey, 'mcpServers');
  assert.equal(typeof entries[0].path, 'string');
  assert.ok(entries[0].path.length > 0);
});

test('parseClaudeMcpListNames extracts server names from list output', () => {
  const output = [
    'Checking MCP server health...',
    '',
    'linear: https://mcp.linear.app/sse - ! Needs authentication',
    'filesystem-local: npx -y @modelcontextprotocol/server-filesystem /tmp - ✓ Connected',
    '',
  ].join('\n');
  const names = parseClaudeMcpListNames(output);
  assert.deepEqual(names, ['linear', 'filesystem-local']);
});

test('parseClaudeMcpGetDetails parses cloud URL connector details and headers', () => {
  const output = [
    'linear:',
    '  Scope: Claude.ai cloud connector',
    '  Status: ! Needs authentication',
    '  Type: sse',
    '  URL: https://mcp.linear.app/sse',
    '  Headers:',
    '    Authorization: Bearer secret-token',
    '',
    'To remove this server, run: claude mcp remove "linear"',
  ].join('\n');

  const details = parseClaudeMcpGetDetails(output);
  assert.equal(details.scope, 'Claude.ai cloud connector');
  assert.equal(details.type, 'sse');
  assert.equal(details.url, 'https://mcp.linear.app/sse');
  assert.deepEqual(details.headers, { Authorization: 'Bearer secret-token' });
});

test('isClaudeCloudScope treats local/user/project as non-cloud', () => {
  assert.equal(isClaudeCloudScope('Local config (private to you in this project)'), false);
  assert.equal(isClaudeCloudScope('User config (available in all your projects)'), false);
  assert.equal(isClaudeCloudScope('Project config (.mcp.json)'), false);
  assert.equal(isClaudeCloudScope('Claude.ai cloud connector'), true);
});

test('doUninstall respects runtime isolation by default', async () => {
  const tempHome = makeTempHome();
  const restoreEnv = setTempHome(tempHome);
  try {
    const configPath = path.join(tempHome, 'test-config.json');
    const overridesDir = path.join(tempHome, '.ultra-lean-mcp-proxy');
    fs.mkdirSync(overridesDir, { recursive: true });
    fs.writeFileSync(
      path.join(overridesDir, 'clients.json'),
      JSON.stringify([{ name: 'test-client', path: configPath, key: 'mcpServers' }], null, 2),
      'utf-8'
    );

    const config = {
      mcpServers: {
        pipTool: {
          command: '/abs/ultra-lean-mcp-proxy',
          args: ['proxy', '--runtime', 'pip', '--', 'npx', 'server-pip'],
        },
        npmTool: {
          command: '/abs/ultra-lean-mcp-proxy',
          args: ['proxy', '--runtime', 'npm', '--', 'npx', 'server-npm'],
        },
      },
    };
    fs.writeFileSync(configPath, JSON.stringify(config, null, 2), 'utf-8');

    await doUninstall({
      clientFilter: 'test-client',
      runtime: 'npm',
      all: false,
      dryRun: false,
      verbose: false,
    });

    const after = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
    assert.equal(isWrapped(after.mcpServers.pipTool), true);
    assert.equal(isWrapped(after.mcpServers.npmTool), false);
    assert.equal(after.mcpServers.npmTool.command, 'npx');
  } finally {
    restoreEnv();
    fs.rmSync(tempHome, { recursive: true, force: true });
  }
});

test('doUninstall restores URL wrapped entries and keeps runtime isolation', async () => {
  const tempHome = makeTempHome();
  const restoreEnv = setTempHome(tempHome);
  try {
    const configPath = path.join(tempHome, 'test-config.json');
    const overridesDir = path.join(tempHome, '.ultra-lean-mcp-proxy');
    fs.mkdirSync(overridesDir, { recursive: true });
    fs.writeFileSync(
      path.join(overridesDir, 'clients.json'),
      JSON.stringify([{ name: 'test-client', path: configPath, key: 'mcpServers' }], null, 2),
      'utf-8'
    );

    const pipOriginal = { url: 'https://pip.example.com/sse', headers: { A: '1' } };
    const npmOriginal = { url: 'https://npm.example.com/sse', headers: { B: '2' } };

    const config = {
      mcpServers: {
        pipUrl: wrapUrlEntry(pipOriginal, '/abs/ultra-lean-mcp-proxy', 'pip'),
        npmUrl: wrapUrlEntry(npmOriginal, '/abs/ultra-lean-mcp-proxy', 'npm'),
      },
    };
    fs.writeFileSync(configPath, JSON.stringify(config, null, 2), 'utf-8');

    await doUninstall({
      clientFilter: 'test-client',
      runtime: 'npm',
      all: false,
      dryRun: false,
      verbose: false,
    });

    const after = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
    assert.equal(isWrapped(after.mcpServers.pipUrl), true);
    assert.equal(isWrapped(after.mcpServers.npmUrl), false);
    assert.deepEqual(after.mcpServers.npmUrl, npmOriginal);
  } finally {
    restoreEnv();
    fs.rmSync(tempHome, { recursive: true, force: true });
  }
});

test('doInstall wraps stdio + URL entries by default and uninstall restores both', async (t) => {
  if (!isUrlBridgeAvailable()) {
    t.skip('npx is not available in PATH');
    return;
  }

  const tempHome = makeTempHome();
  const restoreEnv = setTempHome(tempHome);
  const restorePath = setTempPathWithFakeProxy(tempHome);
  try {
    const configPath = path.join(tempHome, 'test-config.json');
    const overridesDir = path.join(tempHome, '.ultra-lean-mcp-proxy');
    fs.mkdirSync(overridesDir, { recursive: true });
    fs.writeFileSync(
      path.join(overridesDir, 'clients.json'),
      JSON.stringify([{ name: 'test-client', path: configPath, key: 'mcpServers' }], null, 2),
      'utf-8'
    );

    const original = {
      mcpServers: {
        local: { command: 'npx', args: ['server-local'] },
        remote: { url: 'https://mcp.example.com/sse', headers: { Authorization: 'Bearer abc' } },
      },
    };
    fs.writeFileSync(configPath, JSON.stringify(original, null, 2), 'utf-8');

    await doInstall({
      clientFilter: 'test-client',
      dryRun: false,
      offline: true,
      wrapUrl: true,
      verbose: false,
    });

    const wrapped = JSON.parse(fs.readFileSync(configPath, 'utf-8')).mcpServers;
    assert.equal(isWrapped(wrapped.local), true);
    assert.equal(isWrapped(wrapped.remote), true);
    assert.equal(getWrappedTransport(wrapped.remote), 'url');

    await doUninstall({
      clientFilter: 'test-client',
      runtime: 'npm',
      all: false,
      dryRun: false,
      verbose: false,
    });

    const restored = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
    assert.deepEqual(restored, original);
  } finally {
    restorePath();
    restoreEnv();
    fs.rmSync(tempHome, { recursive: true, force: true });
  }
});

test('doInstall supports runtime marker selection for wrappers', async () => {
  const tempHome = makeTempHome();
  const restoreEnv = setTempHome(tempHome);
  const restorePath = setTempPathWithFakeProxy(tempHome);
  try {
    const configPath = path.join(tempHome, 'test-config.json');
    const overridesDir = path.join(tempHome, '.ultra-lean-mcp-proxy');
    fs.mkdirSync(overridesDir, { recursive: true });
    fs.writeFileSync(
      path.join(overridesDir, 'clients.json'),
      JSON.stringify([{ name: 'test-client', path: configPath, key: 'mcpServers' }], null, 2),
      'utf-8'
    );

    const original = {
      mcpServers: {
        local: { command: 'npx', args: ['server-local'] },
      },
    };
    fs.writeFileSync(configPath, JSON.stringify(original, null, 2), 'utf-8');

    await doInstall({
      clientFilter: 'test-client',
      dryRun: false,
      offline: true,
      wrapUrl: false,
      runtime: 'pip',
      verbose: false,
    });

    const wrapped = JSON.parse(fs.readFileSync(configPath, 'utf-8')).mcpServers.local;
    assert.equal(isWrapped(wrapped), true);
    assert.equal(getRuntime(wrapped), 'pip');
  } finally {
    restorePath();
    restoreEnv();
    fs.rmSync(tempHome, { recursive: true, force: true });
  }
});

test('doInstall with wrapUrl=false leaves URL entries unwrapped', async () => {
  const tempHome = makeTempHome();
  const restoreEnv = setTempHome(tempHome);
  const restorePath = setTempPathWithFakeProxy(tempHome);
  try {
    const configPath = path.join(tempHome, 'test-config.json');
    const overridesDir = path.join(tempHome, '.ultra-lean-mcp-proxy');
    fs.mkdirSync(overridesDir, { recursive: true });
    fs.writeFileSync(
      path.join(overridesDir, 'clients.json'),
      JSON.stringify([{ name: 'test-client', path: configPath, key: 'mcpServers' }], null, 2),
      'utf-8'
    );

    const original = {
      mcpServers: {
        local: { command: 'npx', args: ['server-local'] },
        remote: { url: 'https://mcp.example.com/sse' },
      },
    };
    fs.writeFileSync(configPath, JSON.stringify(original, null, 2), 'utf-8');

    await doInstall({
      clientFilter: 'test-client',
      dryRun: false,
      offline: true,
      wrapUrl: false,
      verbose: false,
    });

    const after = JSON.parse(fs.readFileSync(configPath, 'utf-8')).mcpServers;
    assert.equal(isWrapped(after.local), true);
    assert.equal(isWrapped(after.remote), false);
    assert.equal(after.remote.url, 'https://mcp.example.com/sse');
  } finally {
    restorePath();
    restoreEnv();
    fs.rmSync(tempHome, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// isSafePropertyName
// ---------------------------------------------------------------------------

test('isSafePropertyName accepts valid names', () => {
  assert.equal(isSafePropertyName('linear'), true);
  assert.equal(isSafePropertyName('my-server'), true);
  assert.equal(isSafePropertyName('my_server.v2'), true);
  assert.equal(isSafePropertyName('linear-ulmp'), true);
  assert.equal(isSafePropertyName('a'), true);
});

test('isSafePropertyName rejects invalid names', () => {
  assert.equal(isSafePropertyName(''), false);
  assert.equal(isSafePropertyName(null), false);
  assert.equal(isSafePropertyName(undefined), false);
  assert.equal(isSafePropertyName(42), false);
  assert.equal(isSafePropertyName('__proto__'), false);
  assert.equal(isSafePropertyName('constructor'), false);
  assert.equal(isSafePropertyName('prototype'), false);
  assert.equal(isSafePropertyName('has spaces'), false);
  assert.equal(isSafePropertyName('.starts-with-dot'), false);
  assert.equal(isSafePropertyName('-starts-with-dash'), false);
  assert.equal(isSafePropertyName('name\x00bad'), false);
});

// ---------------------------------------------------------------------------
// parseClaudeMcpListNames filters unsafe names
// ---------------------------------------------------------------------------

test('parseClaudeMcpListNames filters out unsafe property names', () => {
  const output = [
    '__proto__: evil',
    'constructor: evil',
    'valid-name: https://example.com - ok',
  ].join('\n');
  const names = parseClaudeMcpListNames(output);
  assert.deepEqual(names, ['valid-name']);
});

test('parseClaudeMcpListNames deduplicates names', () => {
  const output = [
    'linear: https://mcp.linear.app/sse - ok',
    'linear: https://mcp.linear.app/sse - duplicate',
    'other: something else',
  ].join('\n');
  const names = parseClaudeMcpListNames(output);
  assert.deepEqual(names, ['linear', 'other']);
});

// ---------------------------------------------------------------------------
// isClaudeCloudScope rejects unknown scopes
// ---------------------------------------------------------------------------

test('isClaudeCloudScope rejects unknown scopes', () => {
  assert.equal(isClaudeCloudScope('Claude.ai cloud connector'), true);
  assert.equal(isClaudeCloudScope('Some cloud thing'), true);
  assert.equal(isClaudeCloudScope(''), false);
  assert.equal(isClaudeCloudScope(null), false);
  assert.equal(isClaudeCloudScope('Local config (private to you in this project)'), false);
  assert.equal(isClaudeCloudScope('User config (available in all your projects)'), false);
  assert.equal(isClaudeCloudScope('Project config (.mcp.json)'), false);
  // Unknown scope with no 'cloud' keyword is rejected
  assert.equal(isClaudeCloudScope('Unknown new scope'), false);
  assert.equal(isClaudeCloudScope('Experimental beta scope'), false);
});

// ---------------------------------------------------------------------------
// doWrapCloud integration (mock CLI)
// ---------------------------------------------------------------------------

test('doWrapCloud creates wrapped entries from mock cloud connectors', async () => {
  const tempHome = makeTempHome();
  const restoreEnv = setTempHome(tempHome);
  try {
    const configPath = path.join(tempHome, '.claude.json');
    const overridesDir = path.join(tempHome, '.ultra-lean-mcp-proxy');
    fs.mkdirSync(overridesDir, { recursive: true });

    const mockList = [
      'linear: https://mcp.linear.app/sse - ok',
      'local-server: npx server - ok',
    ].join('\n');

    const mockGetLinear = [
      'linear:',
      '  Scope: Claude.ai cloud connector',
      '  Type: sse',
      '  URL: https://mcp.linear.app/sse',
      '  Headers:',
      '    Authorization: Bearer token',
    ].join('\n');

    const mockGetLocal = [
      'local-server:',
      '  Scope: Local config (private to you in this project)',
      '  Type: stdio',
      '  Command: npx server',
    ].join('\n');

    const result = await doWrapCloud({
      dryRun: false,
      runtime: 'npm',
      suffix: '-ulmp',
      verbose: true,
      _commandExists: () => true,
      _runClaudeMcpCommand: (args) => {
        if (args[0] === 'list') return mockList;
        if (args[0] === 'get' && args[1] === 'linear') return mockGetLinear;
        if (args[0] === 'get' && args[1] === 'local-server') return mockGetLocal;
        throw new Error(`unexpected args: ${args.join(' ')}`);
      },
      _resolveProxyPath: () => '/fake/ultra-lean-mcp-proxy',
    });

    assert.equal(result.inspected, 2);
    assert.equal(result.candidates, 1);
    assert.equal(result.written, 1);
    assert.equal(result.skipped, 1);

    const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
    assert.ok(config.mcpServers['linear-ulmp']);
    assert.equal(isWrapped(config.mcpServers['linear-ulmp']), true);
  } finally {
    restoreEnv();
    fs.rmSync(tempHome, { recursive: true, force: true });
  }
});

test('doWrapCloud dry-run does not write files', async () => {
  const tempHome = makeTempHome();
  const restoreEnv = setTempHome(tempHome);
  try {
    const configPath = path.join(tempHome, '.claude.json');

    const mockList = 'linear: https://mcp.linear.app/sse - ok\n';
    const mockGet = [
      'linear:',
      '  Scope: Claude.ai cloud connector',
      '  Type: sse',
      '  URL: https://mcp.linear.app/sse',
    ].join('\n');

    const result = await doWrapCloud({
      dryRun: true,
      runtime: 'npm',
      suffix: '-ulmp',
      _commandExists: () => true,
      _runClaudeMcpCommand: (args) => {
        if (args[0] === 'list') return mockList;
        return mockGet;
      },
      _resolveProxyPath: () => '/fake/proxy',
    });

    assert.equal(result.written, 1);
    assert.equal(fs.existsSync(configPath), false);
  } finally {
    restoreEnv();
    fs.rmSync(tempHome, { recursive: true, force: true });
  }
});

test('doWrapCloud rejects empty suffix', async () => {
  await assert.rejects(
    () => doWrapCloud({ suffix: '', _commandExists: () => true }),
    /non-empty string/
  );
});

test('doWrapCloud throws when claude CLI is missing', async () => {
  await assert.rejects(
    () => doWrapCloud({ _commandExists: () => false }),
    /not found on PATH/
  );
});

// ---------------------------------------------------------------------------
// install + cloud connector discovery integration
// ---------------------------------------------------------------------------

test('install triggers cloud discovery when claude CLI is available', async () => {
  const tempHome = makeTempHome();
  const restoreEnv = setTempHome(tempHome);
  const restorePath = setTempPathWithFakeProxy(tempHome);
  try {
    // Set up a local config for doInstall
    const configPath = path.join(tempHome, 'test-config.json');
    const overridesDir = path.join(tempHome, '.ultra-lean-mcp-proxy');
    fs.mkdirSync(overridesDir, { recursive: true });
    fs.writeFileSync(
      path.join(overridesDir, 'clients.json'),
      JSON.stringify([{ name: 'test-client', path: configPath, key: 'mcpServers' }], null, 2),
      'utf-8'
    );

    const original = {
      mcpServers: {
        local: { command: 'npx', args: ['server-local'] },
      },
    };
    fs.writeFileSync(configPath, JSON.stringify(original, null, 2), 'utf-8');

    // Run doInstall first
    await doInstall({
      clientFilter: 'test-client',
      dryRun: false,
      offline: true,
      wrapUrl: false,
      verbose: false,
    });

    // Verify install worked
    const afterInstall = JSON.parse(fs.readFileSync(configPath, 'utf-8')).mcpServers;
    assert.equal(isWrapped(afterInstall.local), true);

    // Now simulate what the CLI does after install: call doWrapCloud
    const mockList = 'cloud-api: https://api.example.com/mcp - ok\n';
    const mockGet = [
      'cloud-api:',
      '  Scope: Claude.ai cloud connector',
      '  Type: sse',
      '  URL: https://api.example.com/mcp',
    ].join('\n');

    const cloudResult = await doWrapCloud({
      dryRun: false,
      runtime: 'npm',
      suffix: '-ulmp',
      verbose: false,
      _commandExists: () => true,
      _runClaudeMcpCommand: (args) => {
        if (args[0] === 'list') return mockList;
        return mockGet;
      },
      _resolveProxyPath: () => '/fake/ultra-lean-mcp-proxy',
    });

    assert.equal(cloudResult.candidates, 1);
    assert.equal(cloudResult.written, 1);

    // Verify cloud entry was created in ~/.claude.json
    const cloudConfigPath = path.join(tempHome, '.claude.json');
    const cloudConfig = JSON.parse(fs.readFileSync(cloudConfigPath, 'utf-8'));
    assert.ok(cloudConfig.mcpServers['cloud-api-ulmp']);
    assert.equal(isWrapped(cloudConfig.mcpServers['cloud-api-ulmp']), true);
  } finally {
    restorePath();
    restoreEnv();
    fs.rmSync(tempHome, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// cleanEnvForClaude
// ---------------------------------------------------------------------------

test('cleanEnvForClaude strips CLAUDECODE and CLAUDE_CODE', () => {
  const origCLAUDECODE = process.env.CLAUDECODE;
  const origCLAUDE_CODE = process.env.CLAUDE_CODE;
  try {
    process.env.CLAUDECODE = '1';
    process.env.CLAUDE_CODE = '1';
    const env = cleanEnvForClaude();
    assert.equal(env.CLAUDECODE, undefined, 'CLAUDECODE should be stripped');
    assert.equal(env.CLAUDE_CODE, undefined, 'CLAUDE_CODE should be stripped');
    assert.ok(env.PATH || env.Path, 'PATH should be preserved');
  } finally {
    if (origCLAUDECODE !== undefined) process.env.CLAUDECODE = origCLAUDECODE;
    else delete process.env.CLAUDECODE;
    if (origCLAUDE_CODE !== undefined) process.env.CLAUDE_CODE = origCLAUDE_CODE;
    else delete process.env.CLAUDE_CODE;
  }
});

test('cleanEnvForClaude preserves other env vars', () => {
  const env = cleanEnvForClaude();
  assert.equal(typeof env, 'object');
  assert.ok(env.PATH || env.Path, 'PATH should be present');
});

// ---------------------------------------------------------------------------
// parseClaudeMcpListCloudConnectors
// ---------------------------------------------------------------------------

test('parseClaudeMcpListCloudConnectors extracts cloud.ai entries', () => {
  const output = [
    'Checking MCP server health...',
    '',
    'claude.ai Canva: https://mcp.canva.com/mcp - Connected',
    'claude.ai Linear: https://mcp.linear.app/sse - ! Needs authentication',
    'linear: https://mcp.linear.app/sse - ok',
    'local-server: npx server - ok',
    '',
  ].join('\n');
  const results = parseClaudeMcpListCloudConnectors(output);
  assert.equal(results.length, 2);
  assert.equal(results[0].displayName, 'claude.ai Canva');
  assert.equal(results[0].safeName, 'canva');
  assert.equal(results[0].url, 'https://mcp.canva.com/mcp');
  assert.equal(results[0].scope, 'cloud');
  assert.equal(results[0].transport, 'sse');
  assert.equal(results[1].displayName, 'claude.ai Linear');
  assert.equal(results[1].safeName, 'linear');
});

test('parseClaudeMcpListCloudConnectors ignores non-cloud entries', () => {
  const output = [
    'linear: https://mcp.linear.app/sse - ok',
    'local-server: npx server - ok',
  ].join('\n');
  const results = parseClaudeMcpListCloudConnectors(output);
  assert.deepEqual(results, []);
});

test('parseClaudeMcpListCloudConnectors deduplicates by safe name', () => {
  const output = [
    'claude.ai Canva: https://mcp.canva.com/mcp - Connected',
    'claude.ai Canva: https://mcp.canva.com/mcp2 - Duplicate',
  ].join('\n');
  const results = parseClaudeMcpListCloudConnectors(output);
  assert.equal(results.length, 1);
  assert.equal(results[0].url, 'https://mcp.canva.com/mcp');
});

test('parseClaudeMcpListCloudConnectors handles empty input', () => {
  assert.deepEqual(parseClaudeMcpListCloudConnectors(''), []);
  assert.deepEqual(parseClaudeMcpListCloudConnectors(null), []);
});

test('parseClaudeMcpListCloudConnectors handles multi-word service names', () => {
  const output = 'claude.ai Some Cool Service: https://example.com/mcp - Connected\n';
  const results = parseClaudeMcpListCloudConnectors(output);
  assert.equal(results.length, 1);
  assert.equal(results[0].safeName, 'some-cool-service');
});

// ---------------------------------------------------------------------------
// doWrapCloud with cloud.ai entries
// ---------------------------------------------------------------------------

test('doWrapCloud discovers cloud.ai entries from list output', async () => {
  const tempHome = makeTempHome();
  const restoreEnv = setTempHome(tempHome);
  try {
    const configPath = path.join(tempHome, '.claude.json');
    const overridesDir = path.join(tempHome, '.ultra-lean-mcp-proxy');
    fs.mkdirSync(overridesDir, { recursive: true });

    const mockList = [
      'claude.ai Canva: https://mcp.canva.com/mcp - Connected',
      'local-server: npx server - ok',
    ].join('\n');

    const mockGetLocal = [
      'local-server:',
      '  Scope: Local config (private to you in this project)',
      '  Type: stdio',
      '  Command: npx server',
    ].join('\n');

    const result = await doWrapCloud({
      dryRun: false,
      runtime: 'npm',
      suffix: '-ulmp',
      verbose: true,
      _commandExists: () => true,
      _runClaudeMcpCommand: (args) => {
        if (args[0] === 'list') return mockList;
        if (args[0] === 'get' && args[1] === 'local-server') return mockGetLocal;
        // Cloud connectors fail with get
        throw new Error(`No MCP server found: ${args[1]}`);
      },
      _resolveProxyPath: () => '/fake/ultra-lean-mcp-proxy',
    });

    // local-server skipped (local scope), canva discovered via cloud parser
    assert.equal(result.candidates, 1);
    assert.equal(result.written, 1);

    const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
    assert.ok(config.mcpServers['canva-ulmp']);
    assert.equal(isWrapped(config.mcpServers['canva-ulmp']), true);
  } finally {
    restoreEnv();
    fs.rmSync(tempHome, { recursive: true, force: true });
  }
});

test('doWrapCloud works when only cloud.ai entries exist (no standard names)', async () => {
  const tempHome = makeTempHome();
  const restoreEnv = setTempHome(tempHome);
  try {
    const configPath = path.join(tempHome, '.claude.json');
    const overridesDir = path.join(tempHome, '.ultra-lean-mcp-proxy');
    fs.mkdirSync(overridesDir, { recursive: true });

    const mockList = 'claude.ai Canva: https://mcp.canva.com/mcp - Connected\n';

    const result = await doWrapCloud({
      dryRun: false,
      runtime: 'npm',
      suffix: '-ulmp',
      _commandExists: () => true,
      _runClaudeMcpCommand: (args) => {
        if (args[0] === 'list') return mockList;
        throw new Error(`No MCP server found: ${args}`);
      },
      _resolveProxyPath: () => '/fake/proxy',
    });

    assert.equal(result.inspected, 1); // 0 names + 1 cloud connector
    assert.equal(result.candidates, 1);
    assert.equal(result.written, 1);
  } finally {
    restoreEnv();
    fs.rmSync(tempHome, { recursive: true, force: true });
  }
});

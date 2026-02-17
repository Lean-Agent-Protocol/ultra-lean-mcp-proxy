/**
 * Ultra Lean MCP Proxy - config file watcher.
 *
 * Watches discovered MCP client config files for changes and automatically
 * wraps new unwrapped stdio server entries to route through the proxy.
 *
 * Uses fs.watchFile (polling) for cross-platform reliability.
 * Includes file locking with PID-based stale lock recovery.
 *
 * Zero npm dependencies - uses only Node.js built-ins.
 */

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import {
  acquireConfigLock,
  backupConfig,
  getConfigLocations,
  isProcessAlive,
  readConfig,
  releaseConfigLock,
  isWrapped,
  isStdioServer,
  isUrlServer,
  wrapEntry,
  wrapUrlEntry,
  writeConfigAtomic,
  resolveProxyPath,
  isUrlBridgeAvailable,
  parseClaudeMcpListCloudConnectors,
  parseClaudeMcpListNames,
  parseClaudeMcpGetDetails,
  isClaudeCloudScope,
  isClaudeLocalScope,
  isSafePropertyName,
  normalizeClientName,
} from './installer.mjs';

const CONFIG_DIR = path.join(os.homedir(), '.ultra-lean-mcp-proxy');

// ---------------------------------------------------------------------------
// Helpers for cloud discovery
// ---------------------------------------------------------------------------

/**
 * Check if a command exists on the system PATH (watcher-safe: never throws).
 *
 * @param {string} name - Command name to check
 * @returns {boolean}
 */
function commandExistsWatcher(name) {
  const locator = process.platform === 'win32' ? 'where' : 'which';
  try {
    const result = spawnSync(locator, [name], {
      stdio: 'ignore',
      timeout: 5000,
    });
    return result.status === 0;
  } catch {
    return false;
  }
}

/**
 * Run a claude mcp command and return stdout (watcher-safe: never throws).
 *
 * @param {string[]} args - Arguments to pass to `claude mcp`
 * @returns {string|null} - stdout on success, null on failure
 */
function cleanEnvForClaude() {
  const blocked = new Set(['CLAUDECODE', 'CLAUDE_CODE']);
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (!blocked.has(key)) {
      env[key] = value;
    }
  }
  return env;
}

function runClaudeMcpCommandWatcher(args) {
  try {
    const result = spawnSync('claude', ['mcp', ...args], {
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 60000,
      env: cleanEnvForClaude(),
    });
    if (result.error || result.status !== 0) {
      return null;
    }
    return String(result.stdout || '');
  } catch {
    return null;
  }
}

/**
 * Discover cloud connectors from `claude mcp` and merge them into the target config.
 * Watcher-safe: logs warnings but never crashes.
 *
 * @param {string} proxyPath - Absolute path to the proxy binary
 * @param {string} runtime - Runtime identifier
 * @param {string} suffix - Suffix to append to server names
 * @param {boolean} verbose - Verbose logging
 */
async function discoverCloudConnectors(proxyPath, runtime, suffix, verbose) {
  try {
    // List all MCP servers
    const listOutput = runClaudeMcpCommandWatcher(['list']);
    if (!listOutput) {
      if (verbose) {
        process.stderr.write('[watcher] cloud-discovery: failed to run `claude mcp list`\n');
      }
      return;
    }

    const names = parseClaudeMcpListNames(listOutput);
    const cloudConnectors = parseClaudeMcpListCloudConnectors(listOutput);
    if (names.length === 0 && cloudConnectors.length === 0) {
      if (verbose) {
        process.stderr.write('[watcher] cloud-discovery: no servers found\n');
      }
      return;
    }

    // List-then-get flow for standard servers
    const candidates = [];
    for (const name of names) {
      const getOutput = runClaudeMcpCommandWatcher(['get', name]);
      if (!getOutput) {
        if (verbose) {
          process.stderr.write(`[watcher] cloud-discovery: failed to get details for "${name}"\n`);
        }
        continue;
      }

      const details = parseClaudeMcpGetDetails(getOutput);

      // Filter: skip local scope
      if (isClaudeLocalScope(details.scope)) {
        continue;
      }

      // Filter: skip unknown scope
      if (!isClaudeCloudScope(details.scope)) {
        if (verbose) {
          process.stderr.write(`[watcher] cloud-discovery: "${name}" has unknown scope: ${details.scope || 'empty'}\n`);
        }
        continue;
      }

      // Filter: only URL transports
      const transport = String(details.type || '').toLowerCase();
      if (!['sse', 'http', 'streamable-http'].includes(transport)) {
        continue;
      }

      // Filter: must have URL
      if (!details.url) {
        if (verbose) {
          process.stderr.write(`[watcher] cloud-discovery: "${name}" is missing URL\n`);
        }
        continue;
      }

      // Build target name and validate
      const targetName = `${name}${suffix}`;
      if (!isSafePropertyName(targetName)) {
        if (verbose) {
          process.stderr.write(`[watcher] cloud-discovery: target name "${targetName}" is not safe\n`);
        }
        continue;
      }

      // Build wrapped entry
      const sourceEntry = {
        url: details.url,
        transport,
      };
      if (details.headers && Object.keys(details.headers).length > 0) {
        sourceEntry.headers = details.headers;
      }

      const wrappedEntry = wrapUrlEntry(sourceEntry, proxyPath, runtime);
      candidates.push({ targetName, wrappedEntry });
    }

    // Cloud connector entries parsed directly from list output
    const candidateTargetNames = new Set(candidates.map((c) => c.targetName));
    for (const cc of cloudConnectors) {
      const targetName = `${cc.safeName}${suffix}`;
      if (!isSafePropertyName(targetName)) {
        if (verbose) {
          process.stderr.write(`[watcher] cloud-discovery: target name "${targetName}" is not safe\n`);
        }
        continue;
      }
      if (candidateTargetNames.has(targetName)) {
        if (verbose) {
          process.stderr.write(`[watcher] cloud-discovery: "${cc.displayName}" already collected via get\n`);
        }
        continue;
      }

      const sourceEntry = {
        url: cc.url,
        transport: cc.transport,
      };
      const wrappedEntry = wrapUrlEntry(sourceEntry, proxyPath, runtime);
      candidates.push({ targetName, wrappedEntry });
      candidateTargetNames.add(targetName);
    }

    if (candidates.length === 0) {
      if (verbose) {
        process.stderr.write('[watcher] cloud-discovery: no cloud URL connectors to wrap\n');
      }
      return;
    }

    // Find claude-code-user config location
    const locations = await getConfigLocations(true);
    const targetLoc = locations.find((loc) => normalizeClientName(loc.name) === 'claude-code-user') || {
      name: 'claude-code-user',
      path: path.join(os.homedir(), '.claude.json'),
      serverKey: 'mcpServers',
    };
    const configPath = targetLoc.path;
    const serverKey = targetLoc.serverKey || 'mcpServers';

    // Lock, read, merge, write
    if (!acquireConfigLock(configPath)) {
      if (verbose) {
        process.stderr.write('[watcher] cloud-discovery: could not acquire lock\n');
      }
      return;
    }

    try {
      let config = {};
      if (fs.existsSync(configPath)) {
        config = readConfig(configPath);
        if (!config || typeof config !== 'object') {
          config = {};
        }
      }

      if (!config[serverKey] || typeof config[serverKey] !== 'object') {
        config[serverKey] = {};
      }
      const servers = config[serverKey];

      let changed = false;
      for (const candidate of candidates) {
        const existing = servers[candidate.targetName];
        // JSON equality check
        if (existing && JSON.stringify(existing) === JSON.stringify(candidate.wrappedEntry)) {
          continue; // unchanged
        }

        servers[candidate.targetName] = candidate.wrappedEntry;
        changed = true;
        process.stderr.write(`[watcher] cloud-discovery: updated "${candidate.targetName}"\n`);
      }

      if (changed) {
        backupConfig(configPath);
        writeConfigAtomic(configPath, config);
        if (verbose) {
          process.stderr.write(`[watcher] cloud-discovery: config saved: ${configPath}\n`);
        }
      }
    } finally {
      releaseConfigLock(configPath);
    }
  } catch (err) {
    process.stderr.write(`[watcher] cloud-discovery: error: ${err.message}\n`);
  }
}

// ---------------------------------------------------------------------------
// Config change handler
// ---------------------------------------------------------------------------

/**
 * Handle a detected change to a config file. Reads the config, wraps any
 * unwrapped stdio servers, and writes back atomically under a file lock.
 *
 * @param {{name: string, path: string, serverKey: string}} loc
 * @param {string} proxyPath
 * @param {string} runtime
 * @param {boolean} wrapUrl
 * @param {boolean} canWrapUrl
 * @param {boolean} verbose
 */
function handleConfigChange(loc, proxyPath, runtime, wrapUrl, canWrapUrl, verbose) {
  const configPath = loc.path;
  const serverKey = loc.serverKey || 'mcpServers';

  if (!fs.existsSync(configPath)) {
    return;
  }

  if (!acquireConfigLock(configPath)) {
    if (verbose) {
      process.stderr.write(`[watcher] ${loc.name}: could not acquire lock, skipping\n`);
    }
    return;
  }

  try {
    const config = readConfig(configPath);
    if (!config || typeof config !== 'object') return;

    const servers = config[serverKey];
    if (!servers || typeof servers !== 'object') return;

    let changed = false;
    for (const [name, entry] of Object.entries(servers)) {
      const stdio = isStdioServer(entry);
      const url = isUrlServer(entry);
      if (!stdio && !url) continue;
      if (isWrapped(entry)) continue;
      if (url && !wrapUrl) continue;
      if (url && !canWrapUrl) {
        if (verbose) {
          process.stderr.write(`[watcher] ${loc.name}: bridge unavailable for "${name}" (npx missing)\n`);
        }
        continue;
      }

      servers[name] = url
        ? wrapUrlEntry(entry, proxyPath, runtime)
        : wrapEntry(entry, proxyPath, runtime);
      changed = true;
      process.stderr.write(`[watcher] ${loc.name}: wrapped "${name}" (${url ? 'url' : 'stdio'})\n`);
    }

    if (changed) {
      backupConfig(configPath);
      writeConfigAtomic(configPath, config);
    }
  } catch (err) {
    process.stderr.write(`[watcher] ${loc.name}: error processing config: ${err.message}\n`);
  } finally {
    releaseConfigLock(configPath);
  }
}

// ---------------------------------------------------------------------------
// Watch loop
// ---------------------------------------------------------------------------

/**
 * Start watching config files for changes. Performs an initial scan and then
 * polls files at the given interval.
 *
 * @param {object} options
 * @param {number}  options.interval  Polling interval in seconds (default: 5)
 * @param {string}  options.runtime   Runtime identifier (default: 'npm')
 * @param {boolean} options.offline   Skip remote registry fetch (default: false)
 * @param {boolean} options.wrapUrl   Wrap URL entries too (default: true)
 * @param {boolean} options.verbose   Verbose logging (default: false)
 * @param {string}  options.suffix    Suffix for cloud connector names (default: '-ulmp')
 * @param {number}  options.cloudInterval  Cloud discovery interval in seconds (default: 60)
 */
export async function runWatch(options = {}) {
  const { interval = 5, runtime = 'npm', offline = false, wrapUrl = true, verbose = false, suffix = '-ulmp', cloudInterval = 60 } = options;

  const proxyPath = resolveProxyPath();
  const locations = await getConfigLocations(offline);
  const canWrapUrl = wrapUrl ? isUrlBridgeAvailable() : false;
  if (wrapUrl && !canWrapUrl) {
    process.stderr.write('[watcher] URL wrapping enabled but `npx` is unavailable; URL entries will be skipped.\n');
  }
  let watchCount = 0;

  // Check for claude CLI
  const claudeAvailable = commandExistsWatcher('claude');
  if (claudeAvailable) {
    process.stderr.write('[watcher] claude CLI found - cloud discovery enabled\n');
  } else {
    if (verbose) {
      process.stderr.write('[watcher] claude CLI not found - cloud discovery disabled\n');
    }
  }

  // Initial scan
  for (const loc of locations) {
    if (fs.existsSync(loc.path)) {
      handleConfigChange(loc, proxyPath, runtime, wrapUrl, canWrapUrl, verbose);
    }
  }

  // Initial cloud discovery
  if (claudeAvailable) {
    await discoverCloudConnectors(proxyPath, runtime, suffix, verbose);
  }

  // Set up polling watchers (including currently-missing paths, so creation is detected)
  for (const loc of locations) {
    fs.watchFile(loc.path, { interval: interval * 1000 }, (curr, prev) => {
      if (curr.mtimeMs === prev.mtimeMs) return;
      if (verbose) {
        process.stderr.write(`[watcher] ${loc.name}: change detected\n`);
      }
      handleConfigChange(loc, proxyPath, runtime, wrapUrl, canWrapUrl, verbose);
    });
    watchCount++;
  }

  process.stderr.write(
    `[ultra-lean-mcp-proxy] Watching ${watchCount} config files (interval: ${interval}s)\n`
  );

  // Keep the process alive
  const keepAlive = setInterval(() => {}, 60000);

  // Cloud discovery interval
  let cloudDiscoveryInterval = null;
  if (claudeAvailable) {
    cloudDiscoveryInterval = setInterval(() => {
      discoverCloudConnectors(proxyPath, runtime, suffix, verbose);
    }, cloudInterval * 1000);
  }

  // Cleanup on exit
  function cleanup() {
    clearInterval(keepAlive);
    if (cloudDiscoveryInterval) {
      clearInterval(cloudDiscoveryInterval);
    }
    for (const loc of locations) {
      try { fs.unwatchFile(loc.path); } catch { /* ignore */ }
    }
  }

  process.on('SIGINT', () => { cleanup(); process.exit(0); });
  process.on('SIGTERM', () => { cleanup(); process.exit(0); });

  // Return a promise that never resolves (watcher runs until killed)
  return new Promise(() => {});
}

// ---------------------------------------------------------------------------
// Daemon management
// ---------------------------------------------------------------------------

/**
 * Start the watcher as a detached background daemon. Writes its PID to
 * ~/.ultra-lean-mcp-proxy/watch.pid and logs to watch.log.
 *
 * @param {object} options
 * @param {number}  options.interval  Polling interval in seconds
 * @param {string}  options.runtime   Runtime identifier
 * @param {boolean} options.offline   Skip remote registry
 * @param {boolean} options.wrapUrl   Wrap URL entries
 * @param {boolean} options.verbose   Verbose logging
 * @param {string}  options.suffix    Suffix for cloud connector names
 * @param {number}  options.cloudInterval  Cloud discovery interval in seconds
 */
export function startDaemon(options = {}) {
  const { interval = 5, runtime = 'npm', offline = false, wrapUrl = true, verbose = false, suffix = '-ulmp', cloudInterval = 60 } = options;

  fs.mkdirSync(CONFIG_DIR, { recursive: true });
  const pidFile = path.join(CONFIG_DIR, 'watch.pid');
  const logFile = path.join(CONFIG_DIR, 'watch.log');

  // Check if a daemon is already running
  if (fs.existsSync(pidFile)) {
    try {
      const existingPid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim(), 10);
      if (isProcessAlive(existingPid)) {
        process.stderr.write(`[ultra-lean-mcp-proxy] Daemon already running (PID: ${existingPid})\n`);
        return;
      }
      // Stale PID file - clean up
      fs.unlinkSync(pidFile);
    } catch { /* ignore */ }
  }

  // Resolve CLI path from this module's location
  const thisDir = path.dirname(fileURLToPath(import.meta.url));
  const cliPath = path.join(thisDir, '..', 'bin', 'cli.mjs');

  const childArgs = [
    cliPath,
    'watch',
    '--interval', String(interval),
    '--runtime', String(runtime),
    '--suffix', String(suffix),
    '--cloud-interval', String(cloudInterval),
    ...(offline ? ['--offline'] : []),
    ...(wrapUrl ? [] : ['--no-wrap-url']),
    ...(verbose ? ['-v'] : []),
  ];

  const logFd = fs.openSync(logFile, 'a');
  const child = spawn(process.execPath, childArgs, {
    detached: true,
    stdio: ['ignore', logFd, logFd],
  });

  fs.writeFileSync(pidFile, String(child.pid));
  child.unref();
  fs.closeSync(logFd);

  process.stderr.write(`[ultra-lean-mcp-proxy] Daemon started (PID: ${child.pid})\n`);
  process.stderr.write(`[ultra-lean-mcp-proxy] Log file: ${logFile}\n`);
}

/**
 * Stop a running watcher daemon by reading its PID file and sending SIGTERM.
 */
export function stopDaemon() {
  const pidFile = path.join(CONFIG_DIR, 'watch.pid');

  try {
    const pid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim(), 10);
    process.kill(pid, 'SIGTERM');
    fs.unlinkSync(pidFile);
    process.stderr.write(`[ultra-lean-mcp-proxy] Daemon stopped (PID: ${pid})\n`);
  } catch (err) {
    process.stderr.write(`[ultra-lean-mcp-proxy] No daemon running or could not stop: ${err.message}\n`);
  }
}

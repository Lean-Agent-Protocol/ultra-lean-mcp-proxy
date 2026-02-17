/**
 * Installer for Ultra Lean MCP Proxy (Node.js).
 *
 * Discovers MCP client config files, wraps / unwraps server entries to route
 * through the proxy, and handles npx auto-install.
 *
 * Zero npm dependencies - uses only Node.js built-ins.
 */

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { execSync, spawnSync } from 'node:child_process';
import https from 'node:https';
import http from 'node:http';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const REGISTRY_URL = 'https://raw.githubusercontent.com/lean-agent-protocol/ultra-lean-mcp-proxy/main/registry/clients.json';
const REGISTRY_TIMEOUT_MS = 3000;
const REGISTRY_MAX_BYTES = 65536;
const CONFIG_DIR_NAME = '.ultra-lean-mcp-proxy';
const BACKUP_DIR_NAME = '.ultra-lean-mcp-proxy-backups';
const LOCK_RETRIES = 5;
const LOCK_BACKOFF_MS = 200;
const SAFE_PATH_PREFIXES = ['~', '%APPDATA%', '%USERPROFILE%', '$HOME'];
const CLAUDE_LOCAL_SCOPE_PATTERN = /\b(local|user|project)\s+config\b/i;
const CLAUDE_CLOUD_SCOPE_PATTERN = /\bcloud\b/i;
const SAFE_PROPERTY_NAME_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/;
const UNSAFE_PROPERTY_NAMES = new Set(['__proto__', 'constructor', 'prototype']);

/**
 * Validate that a name is safe for use as an object property key.
 * Rejects prototype pollution vectors and invalid characters.
 *
 * @param {string} name
 * @returns {boolean}
 */
export function isSafePropertyName(name) {
  if (typeof name !== 'string' || !name) return false;
  if (UNSAFE_PROPERTY_NAMES.has(name)) return false;
  return SAFE_PROPERTY_NAME_PATTERN.test(name);
}

function sleepMs(ms) {
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  } catch {
    const start = Date.now();
    while (Date.now() - start < ms) {
      // busy-wait fallback for environments without SAB
    }
  }
}

export function normalizeClientName(name) {
  return String(name || '')
    .trim()
    .toLowerCase()
    .replace(/[()]/g, '')
    .replace(/[_\s]+/g, '-');
}

// ---------------------------------------------------------------------------
// Platform-aware config locations
// ---------------------------------------------------------------------------

/**
 * Return the hardcoded default config file paths for known MCP clients on the
 * current platform.
 *
 * @returns {Array<{name: string, path: string, serverKey: string}>}
 */
function getDefaultLocations() {
  const platform = process.platform; // win32 | darwin | linux
  const home = os.homedir();
  const appdata = process.env.APPDATA || path.join(home, 'AppData', 'Roaming');
  const userprofile = process.env.USERPROFILE || home;

  const locations = [];

  // Claude Desktop
  if (platform === 'win32') {
    locations.push({
      name: 'claude-desktop',
      path: path.join(appdata, 'Claude', 'claude_desktop_config.json'),
      serverKey: 'mcpServers',
    });
  } else if (platform === 'darwin') {
    locations.push({
      name: 'claude-desktop',
      path: path.join(home, 'Library', 'Application Support', 'Claude', 'claude_desktop_config.json'),
      serverKey: 'mcpServers',
    });
  } else {
    locations.push({
      name: 'claude-desktop',
      path: path.join(home, '.config', 'claude', 'claude_desktop_config.json'),
      serverKey: 'mcpServers',
    });
  }

  // Claude Code (global settings)
  locations.push({
    name: 'claude-code',
    path: path.join(home, '.claude', 'settings.json'),
    serverKey: 'mcpServers',
  });

  // Claude Code (local settings)
  locations.push({
    name: 'claude-code-local',
    path: path.join(home, '.claude', 'settings.local.json'),
    serverKey: 'mcpServers',
  });

  // Claude Code (new user config used by `claude mcp add --scope user/local`)
  locations.push({
    name: 'claude-code-user',
    path: path.join(home, '.claude.json'),
    serverKey: 'mcpServers',
  });

  // Cursor
  if (platform === 'win32') {
    locations.push({
      name: 'cursor',
      path: path.join(userprofile, '.cursor', 'mcp.json'),
      serverKey: 'mcpServers',
    });
  } else {
    locations.push({
      name: 'cursor',
      path: path.join(home, '.cursor', 'mcp.json'),
      serverKey: 'mcpServers',
    });
  }

  // Windsurf
  if (platform === 'win32') {
    locations.push({
      name: 'windsurf',
      path: path.join(userprofile, '.codeium', 'windsurf', 'mcp_config.json'),
      serverKey: 'mcpServers',
    });
  } else {
    locations.push({
      name: 'windsurf',
      path: path.join(home, '.codeium', 'windsurf', 'mcp_config.json'),
      serverKey: 'mcpServers',
    });
  }

  return locations;
}

function isSafePathTemplate(rawPath) {
  if (typeof rawPath !== 'string' || !rawPath) return false;
  if (rawPath.includes('..')) return false;
  if (/[^\x20-\x7E]/.test(rawPath)) return false;
  return SAFE_PATH_PREFIXES.some((prefix) => rawPath.startsWith(prefix));
}

function expandPathTemplate(rawPath) {
  let expanded = rawPath;
  expanded = expanded.replaceAll('%APPDATA%', process.env.APPDATA || '');
  expanded = expanded.replaceAll('%USERPROFILE%', process.env.USERPROFILE || os.homedir());
  expanded = expanded.replaceAll('$HOME', os.homedir());
  if (expanded.startsWith('~')) {
    const suffix = expanded.slice(1).replace(/^[\\/]+/, '');
    expanded = path.join(os.homedir(), suffix);
  }
  return expanded;
}

/**
 * Fetch the remote client registry payload.
 *
 * @returns {Promise<object|Array| null>}
 */
function fetchRemoteRegistry() {
  return new Promise((resolve) => {
    const protocol = REGISTRY_URL.startsWith('https') ? https : http;
    const req = protocol.get(REGISTRY_URL, { timeout: REGISTRY_TIMEOUT_MS }, (res) => {
      if (res.statusCode < 200 || res.statusCode >= 300) {
        resolve(null);
        res.resume();
        return;
      }

      const chunks = [];
      let totalBytes = 0;

      res.on('data', (chunk) => {
        totalBytes += chunk.length;
        if (totalBytes > REGISTRY_MAX_BYTES) {
          res.destroy();
          resolve(null);
          return;
        }
        chunks.push(chunk);
      });

      res.on('end', () => {
        try {
          const raw = Buffer.concat(chunks).toString('utf-8');
          const parsed = JSON.parse(raw);
          resolve(parsed);
        } catch {
          resolve(null);
        }
      });

      res.on('error', () => resolve(null));
    });

    req.on('error', () => resolve(null));
    req.on('timeout', () => {
      req.destroy();
      resolve(null);
    });
  });
}

/**
 * Read local overrides from ~/.ultra-lean-mcp-proxy/clients.json.
 *
 * @returns {Array}
 */
function readLocalOverrides() {
  const overridePath = path.join(os.homedir(), CONFIG_DIR_NAME, 'clients.json');
  try {
    const raw = fs.readFileSync(overridePath, 'utf-8');
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed;
    if (parsed && typeof parsed === 'object' && Array.isArray(parsed.clients)) return parsed.clients;
    return [];
  } catch {
    return [];
  }
}

/**
 * Normalize registry-like entries into {name, path, serverKey} for this platform.
 *
 * Supports:
 * - bare list: [{name, path, key?}, {name, paths:{...}, key?}]
 * - versioned: {version, clients:[...]}
 *
 * @param {object|Array|null} payload
 * @param {{strict?: boolean}} options
 * @returns {Array<{name: string, path: string, serverKey: string}>}
 */
export function normalizeRegistryEntries(payload, { strict = false } = {}) {
  let clients;
  if (Array.isArray(payload)) {
    clients = payload;
  } else if (payload && typeof payload === 'object' && Array.isArray(payload.clients)) {
    clients = payload.clients;
  } else {
    return [];
  }

  const platform = process.platform;
  const out = [];
  const allowedKeys = new Set(['name', 'paths', 'path', 'key']);

  for (const entry of clients) {
    if (!entry || typeof entry !== 'object') continue;
    if (strict) {
      const keys = Object.keys(entry);
      if (!keys.every((k) => allowedKeys.has(k))) continue;
    }

    const name = normalizeClientName(entry.name);
    if (!name) continue;

    let rawPath = null;
    if (typeof entry.path === 'string') {
      rawPath = entry.path;
    } else if (entry.paths && typeof entry.paths === 'object') {
      rawPath = entry.paths[platform] || null;
    }

    if (typeof rawPath !== 'string' || !rawPath) continue;
    if (strict && !isSafePathTemplate(rawPath)) continue;

    const expandedPath = expandPathTemplate(rawPath);
    out.push({
      name,
      path: expandedPath,
      serverKey: typeof entry.key === 'string' && entry.key ? entry.key : 'mcpServers',
    });
  }

  return out;
}

/**
 * Build the complete list of config locations by merging:
 * 1. Hardcoded platform defaults
 * 2. Remote registry (unless offline)
 * 3. Local overrides
 *
 * Remote entries and local overrides can add new clients or override paths for
 * existing ones (matched by name).
 *
 * @param {boolean} offline  Skip remote registry fetch
 * @returns {Promise<Array<{name: string, path: string, serverKey: string}>>}
 */
export async function getConfigLocations(offline = false) {
  const defaults = getDefaultLocations().map((loc) => ({
    ...loc,
    name: normalizeClientName(loc.name),
  }));
  const locations = defaults;

  // Merge helper: upsert by name
  function mergeIn(extras) {
    for (const entry of extras) {
      if (!entry.name || !entry.path) continue;
      const normalized = {
        ...entry,
        name: normalizeClientName(entry.name),
        path: expandPathTemplate(entry.path),
      };
      const idx = locations.findIndex((l) => l.name === normalized.name);
      if (idx >= 0) {
        locations[idx] = { ...locations[idx], ...normalized };
      } else {
        locations.push({
          serverKey: 'mcpServers',
          ...normalized,
        });
      }
    }
  }

  // Remote registry
  if (!offline) {
    try {
      const remotePayload = await fetchRemoteRegistry();
      const remote = normalizeRegistryEntries(remotePayload, { strict: true });
      mergeIn(remote);
    } catch {
      // fail silently
    }
  }

  // Local overrides
  mergeIn(normalizeRegistryEntries(readLocalOverrides(), { strict: false }));

  return locations;
}

// ---------------------------------------------------------------------------
// JSONC parser (strip comments)
// ---------------------------------------------------------------------------

/**
 * Strip single-line (//) and multi-line comments from a JSONC string.
 *
 * Uses a character-by-character state machine that tracks whether we are inside
 * a string literal (respecting escape sequences).
 *
 * @param {string} text
 * @returns {string}
 */
export function stripJsoncComments(text) {
  const out = [];
  let i = 0;
  let inString = false;
  let escape = false;

  while (i < text.length) {
    const ch = text[i];

    if (escape) {
      out.push(ch);
      escape = false;
      i++;
      continue;
    }

    if (inString) {
      if (ch === '\\') {
        escape = true;
        out.push(ch);
        i++;
        continue;
      }
      if (ch === '"') {
        inString = false;
      }
      out.push(ch);
      i++;
      continue;
    }

    // Not in string
    if (ch === '"') {
      inString = true;
      out.push(ch);
      i++;
      continue;
    }

    // Check for single-line comment
    if (ch === '/' && i + 1 < text.length && text[i + 1] === '/') {
      // Skip to end of line
      i += 2;
      while (i < text.length && text[i] !== '\n') {
        i++;
      }
      continue;
    }

    // Check for multi-line comment
    if (ch === '/' && i + 1 < text.length && text[i + 1] === '*') {
      i += 2;
      while (i + 1 < text.length && !(text[i] === '*' && text[i + 1] === '/')) {
        i++;
      }
      i += 2; // skip closing */
      continue;
    }

    out.push(ch);
    i++;
  }

  return out.join('');
}

// ---------------------------------------------------------------------------
// Config read / write
// ---------------------------------------------------------------------------

/**
 * Read and parse a config file. Supports JSONC (JSON with comments).
 *
 * @param {string} filePath
 * @returns {object|null}  Parsed config or null if file does not exist / is invalid.
 */
export function readConfig(filePath) {
  try {
    const raw = fs.readFileSync(filePath, 'utf-8');
    const stripped = stripJsoncComments(raw);
    return JSON.parse(stripped);
  } catch {
    return null;
  }
}

/**
 * Atomically write JSON data to a file.
 *
 * Writes to a .tmp sibling first, then renames. On Windows, retries once with
 * a 100ms delay if the rename fails (file locking).
 *
 * @param {string} filePath
 * @param {object} data
 */
export function writeConfigAtomic(filePath, data) {
  const dir = path.dirname(filePath);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }

  const tmpPath = filePath + '.tmp';
  const content = JSON.stringify(data, null, 2) + '\n';
  fs.writeFileSync(tmpPath, content, 'utf-8');

  const attempts = process.platform === 'win32' ? 3 : 1;
  let lastErr = null;
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      fs.renameSync(tmpPath, filePath);
      return;
    } catch (err) {
      lastErr = err;
      if (attempt < attempts - 1) {
        sleepMs(100);
      }
    }
  }

  try {
    fs.unlinkSync(tmpPath);
  } catch {
    // ignore cleanup failure
  }
  throw lastErr || new Error(`Failed to atomically write ${filePath}`);
}

/**
 * Create a timestamped backup of a config file.
 *
 * @param {string} filePath
 * @returns {string|null}  Backup file path, or null if source does not exist.
 */
export function backupConfig(filePath) {
  if (!fs.existsSync(filePath)) {
    return null;
  }
  const parent = path.dirname(filePath);
  const backupDir = path.join(parent, BACKUP_DIR_NAME);
  fs.mkdirSync(backupDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..+/, '') + 'Z';
  const base = path.basename(filePath, path.extname(filePath));
  const backupPath = path.join(backupDir, `${base}.${stamp}.bak`);
  fs.copyFileSync(filePath, backupPath);
  return backupPath;
}

export function isProcessAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    if (err && (err.code === 'EPERM' || err.code === 'EACCES')) {
      return true;
    }
    return false;
  }
}

export function acquireConfigLock(configPath, retries = LOCK_RETRIES, backoffMs = LOCK_BACKOFF_MS) {
  const lockPath = `${configPath}.lock`;
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const fd = fs.openSync(lockPath, 'wx');
      fs.writeFileSync(fd, String(process.pid));
      fs.closeSync(fd);
      return true;
    } catch (err) {
      if (!err || err.code !== 'EEXIST') {
        if (attempt < retries - 1) sleepMs(backoffMs);
        continue;
      }

      try {
        const ownerPid = parseInt(fs.readFileSync(lockPath, 'utf-8').trim(), 10);
        if (!isProcessAlive(ownerPid)) {
          fs.unlinkSync(lockPath);
          continue;
        }
      } catch {
        // unreadable lock file; retry
      }

      if (attempt < retries - 1) {
        sleepMs(backoffMs);
      }
    }
  }
  return false;
}

export function releaseConfigLock(configPath) {
  const lockPath = `${configPath}.lock`;
  try {
    fs.unlinkSync(lockPath);
  } catch {
    // ignore
  }
}

/**
 * Lock a config file, read it, call fn(config), backup+write if fn returns an
 * object, and unlock in a finally block.
 *
 * @param {string} configPath
 * @param {(config: object) => object|null} fn  Return a config object to write, or null to skip.
 * @returns {object|null}  The return value of fn.
 */
export function withLockedConfig(configPath, fn) {
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  if (!acquireConfigLock(configPath)) {
    throw new Error(`config is locked by another process: ${configPath}`);
  }
  try {
    let config = {};
    if (fs.existsSync(configPath)) {
      config = readConfig(configPath);
      if (!config || typeof config !== 'object') {
        throw new Error(`could not parse target config: ${configPath}`);
      }
    }
    const result = fn(config);
    if (result && typeof result === 'object') {
      if (fs.existsSync(configPath)) {
        backupConfig(configPath);
      }
      writeConfigAtomic(configPath, result);
    }
    return result;
  } finally {
    releaseConfigLock(configPath);
  }
}

// ---------------------------------------------------------------------------
// Wrap / unwrap detection
// ---------------------------------------------------------------------------

/**
 * Determine whether a server entry is a stdio-based MCP server.
 *
 * @param {object} entry
 * @returns {boolean}
 */
export function isStdioServer(entry) {
  if (typeof entry !== 'object' || entry === null) return false;
  // Explicit transport type check
  if (entry.transport === 'sse' || entry.transport === 'streamable-http') return false;
  if (entry.url) return false; // SSE / HTTP transport
  // Must have a command to be stdio
  return typeof entry.command === 'string' && entry.command.length > 0;
}

export function isUrlServer(entry) {
  if (typeof entry !== 'object' || entry === null) return false;
  return typeof entry.url === 'string' && entry.url.length > 0;
}

function escapeCmdArg(value) {
  return String(value || '')
    .replace(/\^/g, '^^')
    .replace(/[&|<>()!]/g, '^$&');
}

function bridgeCommandForUrl(url) {
  const target = String(url || '').trim();
  if (process.platform === 'win32') {
    // cmd.exe treats URL metacharacters (e.g. &) as control tokens unless escaped.
    return ['cmd', '/c', 'npx', '-y', 'mcp-remote', escapeCmdArg(target)];
  }
  return ['npx', '-y', 'mcp-remote', target];
}

function getArgBeforeSeparator(args, flagName) {
  const dashIdx = args.indexOf('--');
  if (dashIdx < 0) return null;
  for (let i = 1; i < dashIdx; i++) {
    if (args[i] === flagName && i + 1 < dashIdx) {
      return String(args[i + 1]);
    }
  }
  return null;
}

export function getWrappedTransport(entry) {
  if (!isWrapped(entry)) return null;
  const args = Array.isArray(entry.args) ? entry.args : [];
  const transport = getArgBeforeSeparator(args, '--wrapped-transport');
  return transport || 'stdio';
}

function encodeWrappedEntry(entry) {
  return Buffer.from(JSON.stringify(entry), 'utf-8').toString('base64');
}

function decodeWrappedEntry(encoded) {
  if (typeof encoded !== 'string' || !encoded) return null;
  try {
    const json = Buffer.from(encoded, 'base64').toString('utf-8');
    const parsed = JSON.parse(json);
    if (parsed && typeof parsed === 'object') return parsed;
  } catch {
    // ignore
  }
  return null;
}

export function isUrlBridgeAvailable() {
  const locator = process.platform === 'win32' ? 'where' : 'which';
  try {
    const result = spawnSync(locator, ['npx'], {
      stdio: 'ignore',
      timeout: 5000,
    });
    return result.status === 0;
  } catch {
    return false;
  }
}

export function commandExists(commandName) {
  const locator = process.platform === 'win32' ? 'where' : 'which';
  try {
    const result = spawnSync(locator, [commandName], {
      stdio: 'ignore',
      timeout: 5000,
    });
    return result.status === 0;
  } catch {
    return false;
  }
}

/**
 * Return a copy of process.env without keys that block nested Claude CLI calls.
 * @returns {Record<string, string>}
 */
export function cleanEnvForClaude() {
  const blocked = new Set(['CLAUDECODE', 'CLAUDE_CODE']);
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (!blocked.has(key)) {
      env[key] = value;
    }
  }
  return env;
}

function runClaudeMcpCommand(args) {
  const result = spawnSync('claude', ['mcp', ...args], {
    encoding: 'utf-8',
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: 60000,
    env: cleanEnvForClaude(),
  });
  if (result.error) {
    throw new Error(`failed to run 'claude mcp ${args.join(' ')}': ${result.error.message}`);
  }
  if (result.status !== 0) {
    const stderr = String(result.stderr || '').trim();
    const stdout = String(result.stdout || '').trim();
    const detail = stderr || stdout || `exit code ${result.status}`;
    throw new Error(`'claude mcp ${args.join(' ')}' failed: ${detail}`);
  }
  return String(result.stdout || '');
}

/**
 * Parse server names from `claude mcp list` output.
 *
 * @param {string} output
 * @returns {string[]}
 */
export function parseClaudeMcpListNames(output) {
  const names = [];
  const seen = new Set();
  for (const rawLine of String(output || '').split(/\r?\n/)) {
    const line = rawLine.trimEnd();
    const match = line.match(/^([^:\r\n]+):\s+/);
    if (!match) continue;
    const name = match[1].trim();
    if (!name || seen.has(name)) continue;
    if (!isSafePropertyName(name)) continue;
    seen.add(name);
    names.push(name);
  }
  return names;
}

/**
 * Convert a cloud connector display name to a safe property name.
 *
 * "claude.ai Canva" -> "canva"
 * "claude.ai Some Service" -> "some-service"
 *
 * @param {string} displayName
 * @returns {string}
 */
function sanitizeCloudConnectorName(displayName) {
  let cleaned = String(displayName || '').trim().replace(/^claude\.ai\s+/i, '');
  cleaned = cleaned.toLowerCase().trim();
  cleaned = cleaned.replace(/\s+/g, '-');
  cleaned = cleaned.replace(/[^a-z0-9-]/g, '');
  cleaned = cleaned.replace(/^-+|-+$/g, '');
  return cleaned;
}

const CLOUD_CONNECTOR_LINE_PATTERN = /^(claude\.ai\s+[^:]+):\s+(https?:\/\/\S+)\s+-\s+/i;

/**
 * Parse cloud connector entries directly from `claude mcp list` output.
 *
 * Cloud connectors have names like "claude.ai Canva" which fail
 * isSafePropertyName (spaces). This parser extracts them directly
 * from the list output, which already contains the URL.
 *
 * @param {string} output
 * @returns {Array<{displayName: string, safeName: string, url: string, scope: string, transport: string}>}
 */
export function parseClaudeMcpListCloudConnectors(output) {
  const results = [];
  const seen = new Set();
  for (const rawLine of String(output || '').split(/\r?\n/)) {
    const line = rawLine.trimEnd();
    const match = line.match(CLOUD_CONNECTOR_LINE_PATTERN);
    if (!match) continue;
    const displayName = match[1].trim();
    const url = match[2].trim();
    const safeName = sanitizeCloudConnectorName(displayName);
    if (!safeName || seen.has(safeName)) continue;
    if (!isSafePropertyName(safeName)) continue;
    seen.add(safeName);
    results.push({
      displayName,
      safeName,
      url,
      scope: 'cloud',
      transport: 'sse',
    });
  }
  return results;
}

/**
 * Parse details from `claude mcp get <name>` output.
 *
 * @param {string} output
 * @returns {{scope: string|null, type: string|null, url: string|null, command: string|null, args: string|null, headers: Record<string, string>}}
 */
export function parseClaudeMcpGetDetails(output) {
  const info = {
    scope: null,
    type: null,
    url: null,
    command: null,
    args: null,
    headers: {},
  };

  let inHeaders = false;
  for (const rawLine of String(output || '').split(/\r?\n/)) {
    const line = rawLine.trimEnd();
    let match;

    if ((match = line.match(/^\s{2}Scope:\s*(.+)$/))) {
      info.scope = match[1].trim();
      inHeaders = false;
      continue;
    }
    if ((match = line.match(/^\s{2}Type:\s*(.+)$/))) {
      info.type = match[1].trim().toLowerCase();
      inHeaders = false;
      continue;
    }
    if ((match = line.match(/^\s{2}URL:\s*(.+)$/))) {
      info.url = match[1].trim();
      inHeaders = false;
      continue;
    }
    if ((match = line.match(/^\s{2}Command:\s*(.+)$/))) {
      info.command = match[1].trim();
      inHeaders = false;
      continue;
    }
    if ((match = line.match(/^\s{2}Args:\s*(.*)$/))) {
      info.args = match[1].trim();
      inHeaders = false;
      continue;
    }
    if (line.match(/^\s{2}Headers:\s*$/)) {
      inHeaders = true;
      continue;
    }
    if (!inHeaders) continue;

    match = line.match(/^\s{4}([^:]+):\s*(.*)$/);
    if (match) {
      info.headers[match[1].trim()] = match[2].trim();
      continue;
    }
    if (line.trim().length === 0) {
      continue;
    }
    inHeaders = false;
  }

  return info;
}

export function isClaudeLocalScope(scopeLabel) {
  return CLAUDE_LOCAL_SCOPE_PATTERN.test(String(scopeLabel || '').trim());
}

export function isClaudeCloudScope(scopeLabel) {
  const normalized = String(scopeLabel || '').trim();
  if (!normalized) return false;
  if (isClaudeLocalScope(normalized)) return false;
  return CLAUDE_CLOUD_SCOPE_PATTERN.test(normalized);
}

/**
 * Detect whether an MCP server entry is already wrapped by ultra-lean-mcp-proxy.
 *
 * Structural detection checks:
 * - args[0] === "proxy"
 * - args contains "--runtime" followed by a value before "--"
 * - args contains "--" separator
 * - at least one arg after "--"
 *
 * @param {object} entry
 * @returns {boolean}
 */
export function isWrapped(entry) {
  if (typeof entry !== 'object' || entry === null) return false;
  const args = entry.args;
  if (!Array.isArray(args) || args.length === 0) return false;

  // Check: first arg is "proxy"
  if (args[0] !== 'proxy') return false;

  // Check: contains "--" separator
  const dashIdx = args.indexOf('--');
  if (dashIdx < 0) return false;

  // Check: at least one arg after "--"
  if (dashIdx >= args.length - 1) return false;

  // Check: contains "--runtime" with a value before "--"
  let runtimeValue = null;
  for (let i = 1; i < dashIdx; i++) {
    if (args[i] === '--runtime' && i + 1 < dashIdx) {
      runtimeValue = args[i + 1];
      break;
    }
  }
  if (!runtimeValue || !['pip', 'npm'].includes(runtimeValue)) return false;
  return true;
}

/**
 * Extract the runtime value from a wrapped entry.
 *
 * @param {object} entry
 * @returns {string|null}
 */
export function getRuntime(entry) {
  if (!isWrapped(entry)) return null;
  const args = entry.args;
  const dashIdx = args.indexOf('--');
  for (let i = 1; i < dashIdx; i++) {
    if (args[i] === '--runtime' && i + 1 < dashIdx) {
      return args[i + 1];
    }
  }
  return null;
}

/**
 * Wrap a stdio MCP server entry to route through ultra-lean-mcp-proxy.
 *
 * @param {object} entry       Original server entry (command + args)
 * @param {string} proxyPath   Absolute path to the proxy binary
 * @param {string} runtime     Runtime identifier (default: "npm")
 * @returns {object}           New entry with proxy wrapping
 */
export function wrapEntry(entry, proxyPath, runtime = 'npm') {
  const originalCommand = entry.command;
  const originalArgs = Array.isArray(entry.args) ? [...entry.args] : [];

  const newArgs = [
    'proxy',
    '--runtime', runtime,
    '--',
    originalCommand,
    ...originalArgs,
  ];

  const wrapped = { ...entry };
  wrapped.command = proxyPath;
  wrapped.args = newArgs;
  return wrapped;
}

export function wrapUrlEntry(entry, proxyPath, runtime = 'npm') {
  if (isWrapped(entry)) return entry;
  if (!isUrlServer(entry)) return entry;

  const original = JSON.parse(JSON.stringify(entry));
  const encoded = encodeWrappedEntry(original);
  const bridgeArgs = bridgeCommandForUrl(entry.url);

  const wrapped = { ...entry };
  wrapped.command = proxyPath;
  wrapped.args = [
    'proxy',
    '--runtime', runtime,
    '--wrapped-transport', 'url',
    '--wrapped-entry-b64', encoded,
    '--',
    ...bridgeArgs,
  ];
  delete wrapped.url;
  delete wrapped.transport;
  return wrapped;
}

/**
 * Remove proxy wrapping from a server entry, restoring the original command.
 *
 * @param {object} entry  Wrapped server entry
 * @returns {object}      Unwrapped entry with original command restored
 */
export function unwrapEntry(entry) {
  if (!isWrapped(entry)) return entry;

  const args = entry.args;
  const encodedOriginal = getArgBeforeSeparator(args, '--wrapped-entry-b64');
  const wrappedTransport = getArgBeforeSeparator(args, '--wrapped-transport');
  if (wrappedTransport === 'url' && encodedOriginal) {
    const original = decodeWrappedEntry(encodedOriginal);
    if (original && typeof original === 'object') {
      return original;
    }
  }

  const dashIdx = args.indexOf('--');
  const originalArgs = args.slice(dashIdx + 1);

  if (originalArgs.length === 0) return entry;

  const unwrapped = { ...entry };
  unwrapped.command = originalArgs[0];
  unwrapped.args = originalArgs.slice(1);
  return unwrapped;
}

// ---------------------------------------------------------------------------
// npx detection and global install
// ---------------------------------------------------------------------------

/**
 * Detect whether the current process is running via npx (ephemeral cache).
 *
 * @returns {boolean}
 */
export function isNpxContext() {
  const execPath = process.env.npm_execpath || '';
  if (execPath.includes('npx')) return true;

  // Check if running from npm cache directory
  const dir = path.dirname(new URL(import.meta.url).pathname);
  if (dir.includes('_npx') || dir.includes('npm-cache')) return true;

  // On Windows, the URL path may start with /C:/ - normalise
  const normalDir = process.platform === 'win32'
    ? dir.replace(/^\/([A-Za-z]:)/, '$1')
    : dir;
  if (normalDir.includes('_npx') || normalDir.includes('npm-cache')) return true;

  return false;
}

/**
 * Resolve the absolute path to the ultra-lean-mcp-proxy binary.
 *
 * When running via npx, this triggers a global install first.
 *
 * @returns {string}
 */
export function resolveProxyPath() {
  const looksEphemeral = (candidate) => {
    const lower = String(candidate || '').toLowerCase();
    return (
      lower.includes('_npx')
      || lower.includes('npm-cache')
      || lower.includes(`${path.sep}temp${path.sep}`)
      || lower.includes(`${path.sep}tmp${path.sep}`)
      || lower.includes(os.tmpdir().toLowerCase())
    );
  };

  const fromPrefix = (prefix) => {
    if (!prefix) return null;
    const candidates = process.platform === 'win32'
      ? [
          path.join(prefix, 'ultra-lean-mcp-proxy.cmd'),
          path.join(prefix, 'ultra-lean-mcp-proxy'),
          path.join(prefix, 'bin', 'ultra-lean-mcp-proxy.cmd'),
        ]
      : [
          path.join(prefix, 'bin', 'ultra-lean-mcp-proxy'),
          path.join(prefix, 'ultra-lean-mcp-proxy'),
        ];
    for (const candidate of candidates) {
      if (candidate && fs.existsSync(candidate) && !looksEphemeral(candidate)) {
        return candidate;
      }
    }
    return null;
  };

  const fromPathLookup = () => {
    try {
      const cmd = process.platform === 'win32' ? 'where ultra-lean-mcp-proxy' : 'which ultra-lean-mcp-proxy';
      const result = execSync(cmd, {
        encoding: 'utf-8',
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 5000,
      }).trim();
      const firstLine = result.split(/\r?\n/)[0].trim();
      if (firstLine && fs.existsSync(firstLine) && !looksEphemeral(firstLine)) {
        return firstLine;
      }
    } catch {
      // ignore
    }
    return null;
  };

  const getGlobalPrefix = () => {
    const commands = ['npm prefix -g', 'npm config get prefix'];
    for (const command of commands) {
      try {
        const prefix = execSync(command, {
          encoding: 'utf-8',
          stdio: ['ignore', 'pipe', 'pipe'],
          timeout: 10000,
        }).trim();
        if (prefix) return prefix;
      } catch {
        // try next method
      }
    }
    return '';
  };

  // If running via npx, install globally first
  if (isNpxContext()) {
    console.log('[installer] Detected npx context - installing globally for a stable path...');
    try {
      execSync('npm install -g ultra-lean-mcp-proxy', {
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 60000,
      });
    } catch (err) {
      const stderr = err.stderr ? err.stderr.toString().trim() : '';
      throw new Error(`[installer] Failed to install globally: ${stderr || err.message}`);
    }
  }

  const prefixCandidate = fromPrefix(getGlobalPrefix());
  if (prefixCandidate) return prefixCandidate;

  const pathCandidate = fromPathLookup();
  if (pathCandidate) return pathCandidate;

  // Fallback: use the current process entry point if it looks stable
  const selfPath = process.argv[1];
  if (selfPath && !looksEphemeral(selfPath) && fs.existsSync(selfPath)) {
    return selfPath;
  }

  throw new Error(
    '[installer] Could not resolve a stable proxy binary path. '
    + 'Please install globally: npm install -g ultra-lean-mcp-proxy'
  );
}

/**
 * Wrap cloud-scoped Claude MCP URL connectors by mirroring them locally.
 *
 * Reads `claude mcp list/get`, selects cloud scopes,
 * and writes wrapped mirror entries into `~/.claude.json`.
 *
 * @param {object} options
 * @param {boolean} options.dryRun      Print what would change without writing
 * @param {"pip"|"npm"} options.runtime Runtime marker for wrapped entries
 * @param {string} options.suffix       Suffix for mirror server names
 * @param {boolean} options.verbose     Verbose output
 * @param {Function} options._commandExists   Test injection
 * @param {Function} options._runClaudeMcpCommand  Test injection
 * @param {Function} options._resolveProxyPath     Test injection
 * @returns {Promise<object>}
 */
export async function doWrapCloud(options = {}) {
  const {
    dryRun = false,
    runtime = 'npm',
    suffix = '-ulmp',
    verbose = false,
    _commandExists = commandExists,
    _runClaudeMcpCommand = runClaudeMcpCommand,
    _resolveProxyPath = resolveProxyPath,
  } = options;

  if (typeof suffix !== 'string' || !suffix) {
    throw new Error('[wrap-cloud] --suffix must be a non-empty string');
  }

  const selectedRuntime = runtime === 'pip' ? 'pip' : 'npm';

  if (!_commandExists('claude')) {
    throw new Error('[wrap-cloud] `claude` CLI was not found on PATH. Install Claude Code CLI first.');
  }

  const proxyPath = _resolveProxyPath();
  const listOutput = _runClaudeMcpCommand(['list']);
  const names = parseClaudeMcpListNames(listOutput);
  const cloudConnectors = parseClaudeMcpListCloudConnectors(listOutput);

  if (names.length === 0 && cloudConnectors.length === 0) {
    if (listOutput.trim().length > 0) {
      console.warn('[wrap-cloud] Warning: `claude mcp list` produced output but no server names were parsed. The CLI output format may have changed.');
    }
    console.log('[wrap-cloud] No Claude MCP servers found.');
    return {
      inspected: 0,
      candidates: 0,
      written: 0,
      updated: 0,
      unchanged: 0,
      skipped: 0,
      configPath: null,
    };
  }

  const candidates = [];
  let skipped = 0;

  // --- Existing list-then-get flow for local/standard servers ---
  for (const name of names) {
    let details;
    try {
      details = parseClaudeMcpGetDetails(_runClaudeMcpCommand(['get', name]));
    } catch (err) {
      skipped++;
      if (verbose) {
        console.log(`[wrap-cloud]   ${name}: skipped (failed to inspect: ${err.message || err})`);
      }
      continue;
    }

    if (isClaudeLocalScope(details.scope)) {
      skipped++;
      if (verbose) {
        console.log(`[wrap-cloud]   ${name}: skipped (scope is local/user/project)`);
      }
      continue;
    }

    if (!isClaudeCloudScope(details.scope)) {
      skipped++;
      if (verbose) {
        console.log(`[wrap-cloud]   ${name}: skipped (unknown scope: ${details.scope || 'empty'})`);
      }
      continue;
    }

    const transport = String(details.type || '').toLowerCase();
    if (!['sse', 'http', 'streamable-http'].includes(transport)) {
      skipped++;
      if (verbose) {
        console.log(`[wrap-cloud]   ${name}: skipped (cloud scope but non-URL transport: ${transport || 'unknown'})`);
      }
      continue;
    }

    if (!details.url) {
      skipped++;
      if (verbose) {
        console.log(`[wrap-cloud]   ${name}: skipped (cloud URL connector missing URL in CLI output)`);
      }
      continue;
    }

    const targetName = `${name}${suffix}`;
    if (!isSafePropertyName(targetName)) {
      skipped++;
      if (verbose) {
        console.log(`[wrap-cloud]   ${name}: skipped (target name "${targetName}" is not a safe property name)`);
      }
      continue;
    }

    const sourceEntry = {
      url: details.url,
      transport,
    };
    if (details.headers && Object.keys(details.headers).length > 0) {
      sourceEntry.headers = details.headers;
    }

    candidates.push({
      sourceName: name,
      targetName,
      scope: details.scope,
      wrappedEntry: wrapUrlEntry(sourceEntry, proxyPath, selectedRuntime),
    });
  }

  // --- Cloud connector entries parsed directly from list output ---
  const candidateTargetNames = new Set(candidates.map((c) => c.targetName));
  for (const cc of cloudConnectors) {
    const targetName = `${cc.safeName}${suffix}`;
    if (!isSafePropertyName(targetName)) {
      skipped++;
      if (verbose) {
        console.log(`[wrap-cloud]   ${cc.displayName}: skipped (target name "${targetName}" is not safe)`);
      }
      continue;
    }
    if (candidateTargetNames.has(targetName)) {
      if (verbose) {
        console.log(`[wrap-cloud]   ${cc.displayName}: skipped (already collected via get)`);
      }
      continue;
    }

    const sourceEntry = {
      url: cc.url,
      transport: cc.transport,
    };
    candidates.push({
      sourceName: cc.displayName,
      targetName,
      scope: cc.scope,
      wrappedEntry: wrapUrlEntry(sourceEntry, proxyPath, selectedRuntime),
    });
    candidateTargetNames.add(targetName);
  }

  const inspectedCount = names.length + cloudConnectors.length;

  if (candidates.length === 0) {
    console.log('[wrap-cloud] No cloud-scoped URL MCP servers found to wrap.');
    return {
      inspected: inspectedCount,
      candidates: 0,
      written: 0,
      updated: 0,
      unchanged: 0,
      skipped,
      configPath: null,
    };
  }

  const locations = await getConfigLocations(true);
  const targetLoc = locations.find((loc) => normalizeClientName(loc.name) === 'claude-code-user') || {
    name: 'claude-code-user',
    path: path.join(os.homedir(), '.claude.json'),
    serverKey: 'mcpServers',
  };
  const configPath = targetLoc.path;
  const serverKey = targetLoc.serverKey || 'mcpServers';

  let written = 0;
  let updated = 0;
  let unchanged = 0;

  withLockedConfig(configPath, (config) => {
    if (!config[serverKey] || typeof config[serverKey] !== 'object') {
      config[serverKey] = {};
    }
    const servers = config[serverKey];

    for (const candidate of candidates) {
      const existed = Object.prototype.hasOwnProperty.call(servers, candidate.targetName);
      const existing = existed ? servers[candidate.targetName] : undefined;
      if (existing && JSON.stringify(existing) === JSON.stringify(candidate.wrappedEntry)) {
        unchanged++;
        console.log(`[wrap-cloud]   ${candidate.sourceName} -> ${candidate.targetName}: already up to date`);
        continue;
      }

      if (dryRun) {
        const label = existed ? 'Would update' : 'Would create';
        console.log(`[wrap-cloud]   ${candidate.sourceName} -> ${candidate.targetName}: ${label}`);
      } else {
        servers[candidate.targetName] = candidate.wrappedEntry;
        const label = existed ? 'Updated' : 'Created';
        console.log(`[wrap-cloud]   ${candidate.sourceName} -> ${candidate.targetName}: ${label}`);
      }

      if (existed) updated++;
      else written++;
    }

    if (!dryRun && (written > 0 || updated > 0)) {
      console.log(`[wrap-cloud]   Config saved: ${configPath}`);
      return config;
    }
    return null; // no write needed
  });

  console.log('');
  console.log(
    `[wrap-cloud] Done. Inspected: ${inspectedCount}, Cloud URL candidates: ${candidates.length}, `
    + `Created: ${written}, Updated: ${updated}, Unchanged: ${unchanged}, Skipped: ${skipped}`
  );
  if (dryRun) {
    console.log('[wrap-cloud] (dry run - no files were modified)');
  }

  return {
    inspected: inspectedCount,
    candidates: candidates.length,
    written,
    updated,
    unchanged,
    skipped,
    configPath,
  };
}

// ---------------------------------------------------------------------------
// Main operations
// ---------------------------------------------------------------------------

/**
 * Install: wrap MCP server entries in discovered client configs.
 *
 * @param {object} options
 * @param {boolean} options.dryRun      Print what would change without writing
 * @param {string|null} options.clientFilter   Only process this client name
 * @param {string[]|string|null} options.skipNames Skip these server names
 * @param {boolean} options.offline     Skip remote registry fetch
 * @param {boolean} options.wrapUrl     Wrap URL/SSE/HTTP entries (default: true)
 * @param {"pip"|"npm"} options.runtime Runtime marker to write into wrappers
 * @param {boolean} options.verbose     Verbose output
 */
export async function doInstall(options = {}) {
  const {
    dryRun = false,
    clientFilter = null,
    skipNames = [],
    offline = false,
    wrapUrl = true,
    runtime = 'npm',
    verbose = false,
  } = options;

  const selectedRuntime = runtime === 'pip' ? 'pip' : 'npm';

  const proxyPath = resolveProxyPath();
  if (verbose) {
    console.log(`[installer] Proxy binary: ${proxyPath}`);
  }

  const locations = await getConfigLocations(offline);
  const normalizedClientFilter = clientFilter ? normalizeClientName(clientFilter) : null;
  const skipSet = new Set(
    (Array.isArray(skipNames) ? skipNames : skipNames ? [skipNames] : [])
      .map((name) => String(name))
  );
  const canWrapUrl = wrapUrl ? isUrlBridgeAvailable() : false;
  if (wrapUrl && !canWrapUrl) {
    console.warn('[installer] URL wrapping is enabled but `npx` was not found; URL entries will be skipped.');
  }
  let wrapped = 0;
  let skipped = 0;
  let errors = 0;

  for (const loc of locations) {
    // Client filter
    if (normalizedClientFilter && normalizeClientName(loc.name) !== normalizedClientFilter) {
      continue;
    }

    const configPath = loc.path;
    const serverKey = loc.serverKey || 'mcpServers';

    // Check if config file exists
    if (!fs.existsSync(configPath)) {
      if (verbose) {
        console.log(`[installer] ${loc.name}: config not found at ${configPath} -- skipping`);
      }
      continue;
    }

    if (!acquireConfigLock(configPath)) {
      console.error(`  Error: config is locked by another process`);
      errors++;
      continue;
    }

    console.log(`[installer] ${loc.name}: ${configPath}`);

    try {
      if (!fs.existsSync(configPath)) {
        if (verbose) {
          console.log(`[installer] ${loc.name}: config no longer exists -- skipping`);
        }
        continue;
      }

      const config = readConfig(configPath);
      if (!config || typeof config !== 'object') {
        console.error(`  Error: could not parse config`);
        errors++;
        continue;
      }

      const servers = config[serverKey];
      if (!servers || typeof servers !== 'object') {
        if (verbose) {
          console.log(`  No "${serverKey}" section found -- skipping`);
        }
        continue;
      }

      let changed = false;
      for (const [serverName, entry] of Object.entries(servers)) {
        if (skipSet.has(serverName)) {
          if (verbose) {
            console.log(`  ${serverName}: skip list -- skipping`);
          }
          skipped++;
          continue;
        }

        const isStdio = isStdioServer(entry);
        const isUrl = isUrlServer(entry);

        if (!isStdio && !isUrl) {
          if (verbose) {
            console.log(`  ${serverName}: not a wrappable local server -- skipping`);
          }
          skipped++;
          continue;
        }

        if (isWrapped(entry)) {
          if (verbose) {
            console.log(`  ${serverName}: already wrapped -- skipping`);
          }
          skipped++;
          continue;
        }

        if (isUrl && !wrapUrl) {
          if (verbose) {
            console.log(`  ${serverName}: url wrapping disabled (--no-wrap-url) -- skipping`);
          }
          skipped++;
          continue;
        }
        if (isUrl && !canWrapUrl) {
          if (verbose) {
            console.log(`  ${serverName}: bridge dependency unavailable (npx) -- skipping`);
          }
          skipped++;
          continue;
        }

        const newEntry = isUrl
          ? wrapUrlEntry(entry, proxyPath, selectedRuntime)
          : wrapEntry(entry, proxyPath, selectedRuntime);
        if (dryRun) {
          const origin = isUrl ? 'url' : 'stdio';
          console.log(`  ${serverName}: would wrap (${origin})`);
          console.log(`    command: ${newEntry.command}`);
          console.log(`    args: ${JSON.stringify(newEntry.args)}`);
        } else {
          servers[serverName] = newEntry;
          changed = true;
          const origin = isUrl ? 'url' : 'stdio';
          console.log(`  ${serverName}: wrapped (${origin})`);
        }
        wrapped++;
      }

      if (changed && !dryRun) {
        backupConfig(configPath);
        writeConfigAtomic(configPath, config);
        console.log(`  Config saved (backup created)`);
      }
    } finally {
      releaseConfigLock(configPath);
    }
  }

  console.log('');
  console.log(`Done. Wrapped: ${wrapped}, Skipped: ${skipped}, Errors: ${errors}`);
  if (dryRun) {
    console.log('(dry run - no files were modified)');
  }
}

/**
 * Uninstall: unwrap MCP server entries in discovered client configs.
 *
 * @param {object} options
 * @param {boolean} options.dryRun        Print what would change without writing
 * @param {string|null} options.clientFilter   Only process this client name
 * @param {boolean} options.all           Unwrap all runtimes
 * @param {string} options.runtime        Runtime marker to unwrap (default: npm)
 * @param {boolean} options.verbose       Verbose output
 */
export async function doUninstall(options = {}) {
  const {
    dryRun = false,
    clientFilter = null,
    all = false,
    runtime = 'npm',
    verbose = false,
  } = options;

  const locations = await getConfigLocations(true); // always offline for uninstall
  const normalizedClientFilter = clientFilter ? normalizeClientName(clientFilter) : null;
  let unwrapped = 0;
  let skipped = 0;
  let errors = 0;

  for (const loc of locations) {
    if (normalizedClientFilter && normalizeClientName(loc.name) !== normalizedClientFilter) {
      continue;
    }

    const configPath = loc.path;
    const serverKey = loc.serverKey || 'mcpServers';

    if (!fs.existsSync(configPath)) {
      if (verbose) {
        console.log(`[installer] ${loc.name}: config not found at ${configPath} -- skipping`);
      }
      continue;
    }

    if (!acquireConfigLock(configPath)) {
      console.error(`  Error: config is locked by another process`);
      errors++;
      continue;
    }

    console.log(`[installer] ${loc.name}: ${configPath}`);

    try {
      if (!fs.existsSync(configPath)) {
        if (verbose) {
          console.log(`[installer] ${loc.name}: config no longer exists -- skipping`);
        }
        continue;
      }

      const config = readConfig(configPath);
      if (!config || typeof config !== 'object') {
        console.error(`  Error: could not parse config`);
        errors++;
        continue;
      }

      const servers = config[serverKey];
      if (!servers || typeof servers !== 'object') {
        continue;
      }

      let changed = false;
      for (const [serverName, entry] of Object.entries(servers)) {
        if (!isWrapped(entry)) {
          if (verbose) {
            console.log(`  ${serverName}: not wrapped -- skipping`);
          }
          skipped++;
          continue;
        }

        const entryRuntime = getRuntime(entry);
        if (!all && entryRuntime !== runtime) {
          if (verbose) {
            console.log(`  ${serverName}: wrapped for ${entryRuntime}, expected ${runtime} -- skipping`);
          }
          skipped++;
          continue;
        }

        const restored = unwrapEntry(entry);
        if (dryRun) {
          console.log(`  ${serverName}: would unwrap`);
          console.log(`    command: ${restored.command}`);
          console.log(`    args: ${JSON.stringify(restored.args)}`);
        } else {
          servers[serverName] = restored;
          changed = true;
          console.log(`  ${serverName}: unwrapped`);
        }
        unwrapped++;
      }

      if (changed && !dryRun) {
        backupConfig(configPath);
        writeConfigAtomic(configPath, config);
        console.log(`  Config saved (backup created)`);
      }
    } finally {
      releaseConfigLock(configPath);
    }
  }

  console.log('');
  console.log(`Done. Unwrapped: ${unwrapped}, Skipped: ${skipped}, Errors: ${errors}`);
  if (dryRun) {
    console.log('(dry run - no files were modified)');
  }
}

/**
 * Show the current install status for all discovered clients.
 */
export async function showStatus() {
  const locations = await getConfigLocations(true);

  console.log('Ultra Lean MCP Proxy - Status\n');

  let found = false;
  for (const loc of locations) {
    const configPath = loc.path;
    const serverKey = loc.serverKey || 'mcpServers';

    if (!fs.existsSync(configPath)) {
      console.log(`${loc.name}: not found`);
      console.log(`  ${configPath}\n`);
      continue;
    }

    found = true;
    const config = readConfig(configPath);
    if (!config || typeof config !== 'object') {
      console.log(`${loc.name}: error reading config`);
      console.log(`  ${configPath}\n`);
      continue;
    }

    const servers = config[serverKey];
    if (!servers || typeof servers !== 'object' || Object.keys(servers).length === 0) {
      console.log(`${loc.name}: no servers configured`);
      console.log(`  ${configPath}\n`);
      continue;
    }

    console.log(`${loc.name}: ${configPath}`);
    for (const [serverName, entry] of Object.entries(servers)) {
      const wrapped = isWrapped(entry);
      const stdio = isStdioServer(entry);
      const url = isUrlServer(entry);
      let status;
      if (wrapped) {
        const runtime = getRuntime(entry);
        const origin = getWrappedTransport(entry) || 'stdio';
        status = `wrapped (runtime: ${runtime || 'unknown'}, origin=${origin})`;
      } else if (stdio) {
        status = 'not wrapped (origin=stdio)';
      } else if (url) {
        status = 'remote (unwrapped)';
      } else {
        status = 'not wrappable (non-stdio)';
      }
      console.log(`  ${serverName}: ${status}`);
    }
    console.log('');
  }

  if (!found) {
    console.log('No MCP client configs found on this system.');
  }
}

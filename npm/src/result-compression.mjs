/**
 * Generic structured JSON result compression for Ultra Lean MCP Proxy.
 */

import { createHash } from 'node:crypto';
import { cloneJson } from './state.mjs';

function jsonSize(value) {
  return Buffer.byteLength(JSON.stringify(value), 'utf-8');
}

function stableJson(value) {
  return JSON.stringify(value, Object.keys(value || {}).sort());
}

export function makeCompressionOptions(overrides = {}) {
  return {
    mode: 'balanced', // off | balanced | aggressive
    stripNulls: false,
    stripDefaults: false,
    minPayloadBytes: 512,
    enableColumnar: true,
    columnarMinRows: 8,
    columnarMinFields: 2,
    ...overrides,
  };
}

export class TokenCounter {
  constructor() {
    this.backend = 'heuristic';
    this.reason = 'node_builtin_estimator';
  }

  count(value) {
    const text = JSON.stringify(value);
    return Math.max(1, Math.floor(text.length / 4));
  }
}

function collectKeyFrequency(node, counter) {
  if (Array.isArray(node)) {
    for (const item of node) {
      collectKeyFrequency(item, counter);
    }
    return;
  }
  if (node && typeof node === 'object') {
    for (const [key, value] of Object.entries(node)) {
      const name = String(key);
      counter[name] = (counter[name] || 0) + 1;
      collectKeyFrequency(value, counter);
    }
  }
}

function buildKeyAliases(counter, mode) {
  if (mode === 'off') return {};
  const minFreq = mode === 'aggressive' ? 1 : 2;
  const candidates = Object.entries(counter)
    .filter(([key, freq]) => freq >= minFreq && key.length > 2)
    .sort((a, b) => {
      if (a[1] !== b[1]) return b[1] - a[1];
      return b[0].length - a[0].length;
    });

  const aliases = {};
  for (let i = 0; i < candidates.length; i++) {
    const key = candidates[i][0];
    const alias = `k${i}`;
    if (alias.length < key.length) {
      aliases[key] = alias;
    }
  }
  return aliases;
}

function isDefaultish(value) {
  if (value === null || value === '' || value === 0 || value === false) return true;
  if (Array.isArray(value) && value.length === 0) return true;
  if (value && typeof value === 'object' && Object.keys(value).length === 0) return true;
  return false;
}

function canColumnar(items, opts) {
  if (!opts.enableColumnar) return [false, []];
  if (!Array.isArray(items) || items.length < opts.columnarMinRows) return [false, []];
  if (!items.every((item) => item && typeof item === 'object' && !Array.isArray(item))) {
    return [false, []];
  }
  const firstKeys = Object.keys(items[0]);
  if (firstKeys.length < opts.columnarMinFields) return [false, []];
  const firstSet = new Set(firstKeys);
  for (let i = 1; i < items.length; i++) {
    const keys = Object.keys(items[i]);
    if (keys.length !== firstSet.size) return [false, []];
    if (keys.some((key) => !firstSet.has(key))) return [false, []];
  }
  return [true, firstKeys];
}

function encode(node, keyAlias, opts) {
  if (Array.isArray(node)) {
    const [asColumnar, columns] = canColumnar(node, opts);
    if (asColumnar) {
      const encodedColumns = columns.map((col) => keyAlias[col] || col);
      const rows = [];
      for (const item of node) {
        rows.push(columns.map((col) => encode(item[col], keyAlias, opts)));
      }
      return { '~t': { c: encodedColumns, r: rows } };
    }
    return node.map((item) => encode(item, keyAlias, opts));
  }
  if (node && typeof node === 'object') {
    const out = {};
    for (const [key, value] of Object.entries(node)) {
      if (opts.stripNulls && value === null) continue;
      if (
        opts.stripDefaults
        && ['default', 'defaults'].includes(String(key).toLowerCase())
        && isDefaultish(value)
      ) {
        continue;
      }
      const encodedKey = keyAlias[String(key)] || String(key);
      out[encodedKey] = encode(value, keyAlias, opts);
    }
    return out;
  }
  return node;
}

function decode(node, aliasToKey) {
  if (Array.isArray(node)) {
    return node.map((item) => decode(item, aliasToKey));
  }
  if (node && typeof node === 'object') {
    if (node['~t'] && typeof node['~t'] === 'object') {
      const meta = node['~t'];
      const columns = Array.isArray(meta.c) ? meta.c : [];
      const rows = Array.isArray(meta.r) ? meta.r : [];
      const decodedColumns = columns.map((col) => aliasToKey[String(col)] || String(col));
      const items = [];
      for (const row of rows) {
        if (!Array.isArray(row)) continue;
        const item = {};
        for (let i = 0; i < decodedColumns.length; i++) {
          if (i < row.length) {
            item[decodedColumns[i]] = decode(row[i], aliasToKey);
          }
        }
        items.push(item);
      }
      return items;
    }
    const out = {};
    for (const [key, value] of Object.entries(node)) {
      const decodedKey = aliasToKey[String(key)] || String(key);
      out[decodedKey] = decode(value, aliasToKey);
    }
    return out;
  }
  return node;
}

function keyRef(aliasToKey) {
  const digest = createHash('sha256').update(stableJson(aliasToKey), 'utf-8').digest('hex').slice(0, 12);
  return `kdict-${digest}`;
}

export function compressResult(
  inputData,
  options = null,
  {
    keyRegistry = null,
    registryCounter = null,
    reuseKeys = false,
    keyBootstrapInterval = 8,
  } = {}
) {
  const opts = makeCompressionOptions(options || {});
  const originalBytes = jsonSize(inputData);

  if (originalBytes < opts.minPayloadBytes) {
    return {
      encoding: 'lapc-json-v1',
      compressed: false,
      originalBytes,
      compressedBytes: originalBytes,
      savedBytes: 0,
      savedRatio: 0,
      data: inputData,
      keys: {},
    };
  }

  const keyCounter = {};
  collectKeyFrequency(inputData, keyCounter);
  const keyAlias = buildKeyAliases(keyCounter, opts.mode);
  const encoded = encode(inputData, keyAlias, opts);
  const aliasToKey = {};
  for (const [key, alias] of Object.entries(keyAlias)) {
    aliasToKey[alias] = key;
  }

  const envelope = {
    encoding: 'lapc-json-v1',
    compressed: true,
    mode: opts.mode,
    originalBytes,
    data: encoded,
    keys: aliasToKey,
  };

  if (reuseKeys && keyRegistry && typeof keyRegistry === 'object') {
    const ref = keyRef(aliasToKey);
    let includeKeys = true;
    const previous = keyRegistry[ref];
    if (previous && JSON.stringify(previous) === JSON.stringify(aliasToKey)) {
      includeKeys = false;
      if (registryCounter && typeof registryCounter === 'object') {
        const count = (registryCounter[ref] || 0) + 1;
        registryCounter[ref] = count;
        if (keyBootstrapInterval > 0 && count % keyBootstrapInterval === 0) {
          includeKeys = true;
        }
      }
    } else {
      keyRegistry[ref] = cloneJson(aliasToKey);
      if (registryCounter && typeof registryCounter === 'object') {
        registryCounter[ref] = 1;
      }
    }
    envelope.keysRef = ref;
    if (!includeKeys) {
      delete envelope.keys;
    }
  }

  const compressedBytes = jsonSize(envelope);
  const savedBytes = originalBytes - compressedBytes;
  envelope.compressedBytes = compressedBytes;
  envelope.savedBytes = savedBytes;
  envelope.savedRatio = originalBytes > 0 ? savedBytes / originalBytes : 0;

  if (savedBytes <= 0) {
    envelope.compressed = false;
    envelope.data = inputData;
    envelope.keys = {};
    delete envelope.keysRef;
    envelope.compressedBytes = originalBytes;
    envelope.savedBytes = 0;
    envelope.savedRatio = 0;
  }

  return envelope;
}

export function decompressResult(envelope, { keyRegistry = null } = {}) {
  if (!envelope || typeof envelope !== 'object' || envelope.encoding !== 'lapc-json-v1') {
    throw new Error('Unsupported compression envelope');
  }
  if (!envelope.compressed) {
    return envelope.data;
  }
  let keys = envelope.keys;
  if ((!keys || typeof keys !== 'object') && typeof envelope.keysRef === 'string' && keyRegistry) {
    keys = keyRegistry[envelope.keysRef];
  }
  if (!keys || typeof keys !== 'object') {
    throw new Error('Invalid or missing key dictionary in envelope');
  }
  return decode(envelope.data, keys);
}

export function tokenSavings(original, candidate, counter = null) {
  const tc = counter || new TokenCounter();
  return tc.count(original) - tc.count(candidate);
}

export function estimateCompressibility(value) {
  const keyCounter = {};
  const scalarCounter = {};
  let homogeneousLists = 0;
  let totalLists = 0;

  const walk = (node) => {
    if (Array.isArray(node)) {
      totalLists += 1;
      if (node.length > 0 && node.every((item) => item && typeof item === 'object' && !Array.isArray(item))) {
        const keysets = node.map((item) => JSON.stringify(Object.keys(item).sort()));
        if (new Set(keysets).size === 1) {
          homogeneousLists += 1;
        }
      }
      for (const child of node) walk(child);
      return;
    }

    if (node && typeof node === 'object') {
      for (const [key, child] of Object.entries(node)) {
        const name = String(key);
        keyCounter[name] = (keyCounter[name] || 0) + 1;
        walk(child);
      }
      return;
    }

    if (typeof node === 'string' || typeof node === 'number' || typeof node === 'boolean' || node === null) {
      const marker = JSON.stringify(node);
      scalarCounter[marker] = (scalarCounter[marker] || 0) + 1;
    }
  };

  walk(value);

  const totalKeys = Object.values(keyCounter).reduce((acc, n) => acc + n, 0);
  const duplicateKeys = Math.max(0, totalKeys - Object.keys(keyCounter).length);
  const keyRepeatRatio = totalKeys > 0 ? duplicateKeys / totalKeys : 0;

  const totalScalars = Object.values(scalarCounter).reduce((acc, n) => acc + n, 0);
  const duplicateScalars = Math.max(0, totalScalars - Object.keys(scalarCounter).length);
  const scalarRepeatRatio = totalScalars > 0 ? duplicateScalars / totalScalars : 0;

  const homogeneousRatio = totalLists > 0 ? homogeneousLists / totalLists : 0;
  const score = 0.5 * keyRepeatRatio + 0.25 * scalarRepeatRatio + 0.25 * homogeneousRatio;
  if (score < 0) return 0;
  if (score > 1) return 1;
  return score;
}


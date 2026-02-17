/**
 * Session/cache/tool-index state for Ultra Lean MCP Proxy.
 */

import { createHash } from 'node:crypto';

function isPlainObject(value) {
  return Object.prototype.toString.call(value) === '[object Object]';
}

export function canonicalize(value) {
  if (Array.isArray(value)) {
    return value.map((item) => canonicalize(item));
  }
  if (isPlainObject(value)) {
    const out = {};
    const keys = Object.keys(value).sort();
    for (const key of keys) {
      out[key] = canonicalize(value[key]);
    }
    return out;
  }
  return value;
}

export function cloneJson(value) {
  if (value === undefined) return undefined;
  return JSON.parse(JSON.stringify(value));
}

export function stableJsonStringify(value) {
  return JSON.stringify(canonicalize(value));
}

export function stableHash(value) {
  return createHash('sha256').update(stableJsonStringify(value), 'utf-8').digest('hex');
}

export function argsHash(argumentsValue) {
  if (argumentsValue === null || argumentsValue === undefined) {
    return stableHash({});
  }
  return stableHash(argumentsValue);
}

const MUTATING_VERBS = [
  'create',
  'update',
  'delete',
  'remove',
  'set',
  'write',
  'insert',
  'patch',
  'post',
  'put',
  'merge',
  'upload',
  'commit',
  'navigate',
  'open',
  'close',
  'click',
  'type',
  'press',
  'select',
  'hover',
  'drag',
  'drop',
  'scroll',
  'evaluate',
  'execute',
  'goto',
  'reload',
  'back',
  'forward',
];

export function isMutatingToolName(toolName) {
  const name = String(toolName || '').toLowerCase();
  return MUTATING_VERBS.some((verb) => name.includes(verb));
}

export function makeCacheKey(sessionId, serverName, toolName, argumentsValue) {
  return `${sessionId}:${serverName}:${toolName}:${argsHash(argumentsValue)}`;
}

function nowSeconds() {
  return Date.now() / 1000;
}

function toolSearchTerms(query) {
  return String(query || '')
    .toLowerCase()
    .match(/[a-zA-Z0-9_]+/g) || [];
}

export class ProxyState {
  constructor(maxCacheEntries = 5000) {
    this.maxCacheEntries = Math.max(1, Number(maxCacheEntries) || 5000);
    this._cache = new Map(); // key -> {value, expiresAt, createdAt, hits}
    this._history = new Map(); // key -> json
    this._tools = [];
    this._toolsHash = new Map(); // key -> {lastHash, conditionalHits, updatedAt}
  }

  cacheGet(key) {
    const entry = this._cache.get(key);
    if (!entry) return null;
    if (entry.expiresAt < nowSeconds()) {
      this._cache.delete(key);
      return null;
    }
    entry.hits += 1;
    return cloneJson(entry.value);
  }

  cacheSet(key, value, ttlSeconds) {
    const now = nowSeconds();
    this._cache.set(key, {
      value: cloneJson(value),
      createdAt: now,
      expiresAt: now + Math.max(0, Number(ttlSeconds) || 0),
      hits: 0,
    });
    this._evictCacheIfNeeded();
  }

  cacheInvalidatePrefix(prefix) {
    let removed = 0;
    for (const key of this._cache.keys()) {
      if (key.startsWith(prefix)) {
        this._cache.delete(key);
        removed += 1;
      }
    }
    return removed;
  }

  _evictCacheIfNeeded() {
    if (this._cache.size <= this.maxCacheEntries) return;
    const ordered = Array.from(this._cache.entries()).sort((a, b) => {
      const ah = a[1].hits - b[1].hits;
      if (ah !== 0) return ah;
      return a[1].createdAt - b[1].createdAt;
    });
    const overflow = this._cache.size - this.maxCacheEntries;
    for (let i = 0; i < overflow; i++) {
      this._cache.delete(ordered[i][0]);
    }
  }

  historyGet(key) {
    if (!this._history.has(key)) return null;
    return cloneJson(this._history.get(key));
  }

  historySet(key, value) {
    this._history.set(key, cloneJson(value));
    if (this._history.size > this.maxCacheEntries * 2) {
      const firstKey = this._history.keys().next().value;
      if (firstKey !== undefined) {
        this._history.delete(firstKey);
      }
    }
  }

  historyInvalidatePrefix(prefix) {
    let removed = 0;
    for (const key of this._history.keys()) {
      if (key.startsWith(prefix)) {
        this._history.delete(key);
        removed += 1;
      }
    }
    return removed;
  }

  setTools(tools) {
    this._tools = cloneJson(Array.isArray(tools) ? tools : []);
  }

  getTools() {
    return cloneJson(this._tools);
  }

  searchTools(query, topK = 8, includeSchemas = true) {
    if (!Array.isArray(this._tools) || this._tools.length === 0) {
      return [];
    }

    const terms = toolSearchTerms(query);
    const queryLower = String(query || '').toLowerCase();
    const ranked = [];

    for (const tool of this._tools) {
      if (!isPlainObject(tool)) continue;
      const name = String(tool.name || '');
      const desc = String(tool.description || '');
      const schema = (isPlainObject(tool.inputSchema) && tool.inputSchema)
        || (isPlainObject(tool.input_schema) && tool.input_schema)
        || {};
      const properties = isPlainObject(schema.properties) ? schema.properties : {};
      const paramText = Object.keys(properties).join(' ');
      const haystack = `${name} ${desc} ${paramText}`.toLowerCase();

      let score = 0;
      if (queryLower && name.toLowerCase().includes(queryLower)) score += 4;
      for (const term of terms) {
        if (name.toLowerCase().includes(term)) score += 2;
        if (desc.toLowerCase().includes(term)) score += 1;
        if (paramText.toLowerCase().includes(term)) score += 1.25;
        if (haystack.includes(term)) score += 0.2;
      }
      if (score <= 0) continue;
      ranked.push([score, tool]);
    }

    const fallbackRanked = ranked.length > 0
      ? ranked
      : this._tools.map((tool) => [0.01, tool]);

    fallbackRanked.sort((a, b) => b[0] - a[0]);
    const limit = Math.max(1, Number(topK) || 8);
    const out = [];
    for (const [score, tool] of fallbackRanked.slice(0, limit)) {
      const item = {
        name: tool.name,
        score: Number(score.toFixed(3)),
        description: tool.description || '',
      };
      if (includeSchemas) {
        const schema = tool.inputSchema || tool.input_schema;
        if (schema !== undefined) {
          item.inputSchema = cloneJson(schema);
        }
      }
      out.push(item);
    }
    return out;
  }

  toolsHashGet(key) {
    const entry = this._toolsHash.get(key);
    if (!entry) return null;
    return {
      lastHash: entry.lastHash ?? null,
      conditionalHits: Number(entry.conditionalHits || 0),
      updatedAt: Number(entry.updatedAt || 0),
    };
  }

  toolsHashSetLast(key, toolsHash) {
    const now = nowSeconds();
    const current = this._toolsHash.get(key) || {
      lastHash: null,
      conditionalHits: 0,
      updatedAt: 0,
    };
    if (current.lastHash !== toolsHash) {
      current.conditionalHits = 0;
    }
    current.lastHash = toolsHash;
    current.updatedAt = now;
    this._toolsHash.set(key, current);
  }

  toolsHashRecordHit(key) {
    const now = nowSeconds();
    const current = this._toolsHash.get(key) || {
      lastHash: null,
      conditionalHits: 0,
      updatedAt: 0,
    };
    current.conditionalHits += 1;
    current.updatedAt = now;
    this._toolsHash.set(key, current);
    return current.conditionalHits;
  }

  toolsHashResetHits(key) {
    const now = nowSeconds();
    const current = this._toolsHash.get(key) || {
      lastHash: null,
      conditionalHits: 0,
      updatedAt: 0,
    };
    current.conditionalHits = 0;
    current.updatedAt = now;
    this._toolsHash.set(key, current);
  }
}


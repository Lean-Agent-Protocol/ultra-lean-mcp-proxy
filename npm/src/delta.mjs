/**
 * Delta response helpers for Ultra Lean MCP Proxy.
 */

import { createHash } from 'node:crypto';
import { cloneJson, canonicalize } from './state.mjs';

function jsonBytes(value) {
  return Buffer.byteLength(JSON.stringify(value), 'utf-8');
}

export function stableHash(value) {
  const text = JSON.stringify(canonicalize(value));
  return createHash('sha256').update(text, 'utf-8').digest('hex');
}

function isObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value);
}

function deepEqual(a, b) {
  return JSON.stringify(canonicalize(a)) === JSON.stringify(canonicalize(b));
}

function diffValues(previous, current, path, ops) {
  if (deepEqual(previous, current)) {
    return;
  }

  if (Array.isArray(previous) && Array.isArray(current)) {
    if (previous.length !== current.length) {
      ops.push({ op: 'set', path, value: cloneJson(current) });
      return;
    }
    for (let i = 0; i < current.length; i++) {
      diffValues(previous[i], current[i], [...path, i], ops);
    }
    return;
  }

  if (isObject(previous) && isObject(current)) {
    const keys = new Set([...Object.keys(previous), ...Object.keys(current)]);
    for (const key of Array.from(keys).sort()) {
      if (!(key in current)) {
        ops.push({ op: 'delete', path: [...path, key] });
        continue;
      }
      if (!(key in previous)) {
        ops.push({ op: 'set', path: [...path, key], value: cloneJson(current[key]) });
        continue;
      }
      diffValues(previous[key], current[key], [...path, key], ops);
    }
    return;
  }

  ops.push({ op: 'set', path, value: cloneJson(current) });
}

export function createDelta(
  previous,
  current,
  minSavingsRatio = 0.15,
  maxPatchBytes = 65536
) {
  const canonicalPrevious = canonicalize(previous);
  const canonicalCurrent = canonicalize(current);
  if (deepEqual(canonicalPrevious, canonicalCurrent)) {
    return null;
  }

  const ops = [];
  diffValues(canonicalPrevious, canonicalCurrent, [], ops);
  if (ops.length === 0) {
    return null;
  }

  const patchBytes = jsonBytes(ops);
  const fullBytes = jsonBytes(canonicalCurrent);
  if (patchBytes > maxPatchBytes) {
    return null;
  }

  const savingsRatio = fullBytes > 0 ? (fullBytes - patchBytes) / fullBytes : 0;
  if (savingsRatio < minSavingsRatio) {
    return null;
  }

  return {
    encoding: 'lapc-delta-v1',
    baselineHash: stableHash(canonicalPrevious),
    currentHash: stableHash(canonicalCurrent),
    ops,
    patchBytes,
    fullBytes,
    savedBytes: fullBytes - patchBytes,
    savedRatio: savingsRatio,
  };
}

function getParentForPath(root, path) {
  if (!Array.isArray(path) || path.length === 0) {
    return { parent: null, key: null };
  }
  let cursor = root;
  for (let i = 0; i < path.length - 1; i++) {
    const segment = path[i];
    const nextSegment = path[i + 1];
    if (Array.isArray(cursor)) {
      const idx = Number(segment);
      if (!Number.isInteger(idx) || idx < 0) {
        throw new Error('Invalid array index in delta path');
      }
      if (cursor[idx] === undefined) {
        cursor[idx] = Number.isInteger(Number(nextSegment)) ? [] : {};
      }
      cursor = cursor[idx];
      continue;
    }
    if (!isObject(cursor)) {
      throw new Error('Invalid delta path parent');
    }
    if (!(segment in cursor) || cursor[segment] === null || cursor[segment] === undefined) {
      cursor[segment] = Number.isInteger(Number(nextSegment)) ? [] : {};
    }
    cursor = cursor[segment];
  }
  return { parent: cursor, key: path[path.length - 1] };
}

export function applyDelta(previous, delta) {
  if (!delta || typeof delta !== 'object' || delta.encoding !== 'lapc-delta-v1') {
    throw new Error('Unsupported delta envelope');
  }
  const ops = Array.isArray(delta.ops) ? delta.ops : null;
  if (!ops) {
    throw new Error('Delta envelope missing ops');
  }

  let output = cloneJson(previous);
  for (const op of ops) {
    if (!op || typeof op !== 'object' || !Array.isArray(op.path)) {
      throw new Error('Invalid delta op');
    }
    const path = op.path;
    if (op.op === 'set') {
      if (path.length === 0) {
        output = cloneJson(op.value);
        continue;
      }
      const { parent, key } = getParentForPath(output, path);
      if (Array.isArray(parent)) {
        const idx = Number(key);
        if (!Number.isInteger(idx) || idx < 0) {
          throw new Error('Invalid array index in set op');
        }
        parent[idx] = cloneJson(op.value);
      } else if (isObject(parent)) {
        parent[key] = cloneJson(op.value);
      } else {
        throw new Error('Invalid set op parent');
      }
      continue;
    }

    if (op.op === 'delete') {
      if (path.length === 0) {
        output = null;
        continue;
      }
      const { parent, key } = getParentForPath(output, path);
      if (Array.isArray(parent)) {
        const idx = Number(key);
        if (!Number.isInteger(idx) || idx < 0 || idx >= parent.length) {
          continue;
        }
        parent.splice(idx, 1);
      } else if (isObject(parent)) {
        delete parent[key];
      }
      continue;
    }

    throw new Error(`Unsupported delta op: ${op.op}`);
  }
  return output;
}


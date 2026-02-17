/**
 * Helpers for tools_hash_sync MCP extension.
 */

import { createHash } from 'node:crypto';
import { canonicalize } from './state.mjs';

const TOOLS_HASH_WIRE_RE = /^([a-z0-9_]+):([0-9a-f]{64})$/;

export function canonicalToolsJson(toolsPayload) {
  return JSON.stringify(canonicalize(toolsPayload));
}

export function computeToolsHash(
  toolsPayload,
  {
    algorithm = 'sha256',
    includeServerFingerprint = false,
    serverFingerprint = null,
  } = {}
) {
  if (algorithm !== 'sha256') {
    throw new Error(`Unsupported tools hash algorithm: ${algorithm}`);
  }

  const payload = canonicalize(toolsPayload);
  let preimage = payload;
  if (includeServerFingerprint) {
    preimage = {
      tools: payload,
      server_fingerprint: serverFingerprint || '',
    };
  }
  const digest = createHash('sha256').update(JSON.stringify(preimage), 'utf-8').digest('hex');
  return `sha256:${digest}`;
}

export function parseIfNoneMatch(value, { expectedAlgorithm = 'sha256' } = {}) {
  if (typeof value !== 'string') {
    return null;
  }
  const candidate = value.trim().toLowerCase();
  const match = candidate.match(TOOLS_HASH_WIRE_RE);
  if (!match) {
    return null;
  }
  if (match[1] !== expectedAlgorithm) {
    return null;
  }
  return candidate;
}


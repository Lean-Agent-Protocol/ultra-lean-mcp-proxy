import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  canonicalToolsJson,
  computeToolsHash,
  parseIfNoneMatch,
} from '../src/tools-hash-sync.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

test('canonicalToolsJson and computeToolsHash are stable for key order changes', () => {
  const toolsA = [{ name: 'x', inputSchema: { type: 'object', properties: { a: { type: 'string' } } } }];
  const toolsB = [{ inputSchema: { properties: { a: { type: 'string' }, }, type: 'object' }, name: 'x' }];
  assert.equal(canonicalToolsJson(toolsA), canonicalToolsJson(toolsB));
  assert.equal(computeToolsHash(toolsA), computeToolsHash(toolsB));
});

test('server fingerprint binding changes hash value', () => {
  const tools = [{ name: 'list_items', inputSchema: { type: 'object', properties: {} } }];
  const plain = computeToolsHash(tools, { includeServerFingerprint: false });
  const boundA = computeToolsHash(tools, { includeServerFingerprint: true, serverFingerprint: 'srv-a' });
  const boundB = computeToolsHash(tools, { includeServerFingerprint: true, serverFingerprint: 'srv-b' });
  assert.notEqual(plain, boundA);
  assert.notEqual(boundA, boundB);
});

test('parseIfNoneMatch validates wire format', () => {
  const valid = `sha256:${'a'.repeat(64)}`;
  assert.equal(parseIfNoneMatch(valid), valid);
  assert.equal(parseIfNoneMatch(valid.toUpperCase()), valid);
  assert.equal(parseIfNoneMatch(`sha1:${'a'.repeat(64)}`), null);
  assert.equal(parseIfNoneMatch('sha256:zzzz'), null);
  assert.equal(parseIfNoneMatch(123), null);
});

test('shared fixture produces valid tools hash wire format', () => {
  const fixturePath = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'v2_tools_list_sample.json');
  const payload = JSON.parse(fs.readFileSync(fixturePath, 'utf-8'));
  const wire = computeToolsHash(payload.tools);
  assert.ok(wire.startsWith('sha256:'));
  assert.equal(wire.length, 'sha256:'.length + 64);
});

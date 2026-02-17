import test from 'node:test';
import assert from 'node:assert/strict';

import { ProxyState, isMutatingToolName, makeCacheKey } from '../src/state.mjs';

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

test('cache set/get respects ttl expiration', async () => {
  const state = new ProxyState(10);
  const key = makeCacheKey('s1', 'srv', 'list_items', { page: 1 });
  state.cacheSet(key, { ok: true }, 0);
  await sleep(5);
  assert.equal(state.cacheGet(key), null);
});

test('cache returns cloned values', () => {
  const state = new ProxyState(10);
  const key = makeCacheKey('s1', 'srv', 'list_items', { page: 1 });
  state.cacheSet(key, { nested: { value: 1 } }, 60);
  const cached = state.cacheGet(key);
  cached.nested.value = 999;
  const cached2 = state.cacheGet(key);
  assert.equal(cached2.nested.value, 1);
});

test('searchTools returns ranked matches', () => {
  const state = new ProxyState(10);
  state.setTools([
    {
      name: 'list_pull_requests',
      description: 'List pull requests for repo',
      inputSchema: { type: 'object', properties: { repo: { type: 'string' } } },
    },
    {
      name: 'create_issue',
      description: 'Create an issue in repository',
      inputSchema: { type: 'object', properties: { title: { type: 'string' } } },
    },
  ]);

  const matches = state.searchTools('pull requests', 2, false);
  assert.ok(matches.length > 0);
  assert.equal(matches[0].name, 'list_pull_requests');
  assert.equal(Object.prototype.hasOwnProperty.call(matches[0], 'inputSchema'), false);
});

test('tools hash state tracks last hash and conditional hits', () => {
  const state = new ProxyState(10);
  const key = 'session:server:profile';

  assert.equal(state.toolsHashGet(key), null);
  state.toolsHashSetLast(key, 'sha256:abc');
  let entry = state.toolsHashGet(key);
  assert.equal(entry.lastHash, 'sha256:abc');
  assert.equal(entry.conditionalHits, 0);

  assert.equal(state.toolsHashRecordHit(key), 1);
  assert.equal(state.toolsHashRecordHit(key), 2);
  entry = state.toolsHashGet(key);
  assert.equal(entry.conditionalHits, 2);

  state.toolsHashSetLast(key, 'sha256:def');
  entry = state.toolsHashGet(key);
  assert.equal(entry.lastHash, 'sha256:def');
  assert.equal(entry.conditionalHits, 0);
});

test('isMutatingToolName includes stateful browser actions', () => {
  assert.equal(isMutatingToolName('puppeteer_navigate'), true);
  assert.equal(isMutatingToolName('puppeteer_evaluate'), true);
  assert.equal(isMutatingToolName('create_issue'), true);
  assert.equal(isMutatingToolName('read_graph'), false);
});


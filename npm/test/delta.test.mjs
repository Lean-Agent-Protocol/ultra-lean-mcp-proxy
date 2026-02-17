import test from 'node:test';
import assert from 'node:assert/strict';

import { applyDelta, createDelta } from '../src/delta.mjs';

test('createDelta returns null when payload is unchanged', () => {
  const payload = { items: [{ id: 1, status: 'open' }] };
  assert.equal(createDelta(payload, payload, 0), null);
});

test('createDelta + applyDelta roundtrip reconstructs current payload', () => {
  const previous = {
    items: [
      { id: 1, status: 'open', title: 'alpha' },
      { id: 2, status: 'open', title: 'beta' },
    ],
    count: 2,
  };
  const current = {
    items: [
      { id: 1, status: 'closed', title: 'alpha' },
      { id: 2, status: 'open', title: 'beta' },
    ],
    count: 2,
  };
  const delta = createDelta(previous, current, 0);
  assert.ok(delta);
  const reconstructed = applyDelta(previous, delta);
  assert.deepEqual(reconstructed, current);
});


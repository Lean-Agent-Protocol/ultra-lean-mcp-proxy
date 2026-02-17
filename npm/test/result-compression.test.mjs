import test from 'node:test';
import assert from 'node:assert/strict';

import {
  compressResult,
  decompressResult,
  estimateCompressibility,
  makeCompressionOptions,
} from '../src/result-compression.mjs';

test('compressResult + decompressResult roundtrip', () => {
  const data = {
    repositories: [
      {
        repository_name: 'alpha',
        repository_description: 'Primary repository',
        repository_owner: 'team-a',
      },
      {
        repository_name: 'beta',
        repository_description: 'Secondary repository',
        repository_owner: 'team-b',
      },
      {
        repository_name: 'gamma',
        repository_description: 'Tertiary repository',
        repository_owner: 'team-c',
      },
    ],
  };
  const envelope = compressResult(data, makeCompressionOptions({ mode: 'aggressive', minPayloadBytes: 0 }));
  const reconstructed = decompressResult(envelope);
  assert.deepEqual(reconstructed, data);
});

test('small payload remains uncompressed', () => {
  const payload = { a: 1 };
  const envelope = compressResult(payload, makeCompressionOptions({ minPayloadBytes: 1024 }));
  assert.equal(envelope.compressed, false);
  assert.deepEqual(envelope.data, payload);
  assert.deepEqual(decompressResult(envelope), payload);
});

test('compressibility score higher for repetitive payloads', () => {
  const repetitive = {
    items: Array.from({ length: 30 }, () => ({ service: 'api', region: 'us-east-1', status: 'ok' })),
  };
  const diverse = {
    items: Array.from({ length: 30 }, (_, i) => ({ id: i, name: `n${i}`, value: i * 13 })),
  };
  assert.ok(estimateCompressibility(repetitive) > estimateCompressibility(diverse));
});


import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const CLI_PATH = path.join(__dirname, '..', 'bin', 'cli.mjs');

function writeTempUpstream() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ulmp-node-upstream-'));
  const file = path.join(dir, 'upstream.mjs');
  const script = `
import readline from 'node:readline';

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + '\\n');
}

for await (const line of rl) {
  const trimmed = line.trim();
  if (!trimmed) continue;
  let msg;
  try {
    msg = JSON.parse(trimmed);
  } catch {
    continue;
  }
  const id = msg.id;
  if (msg.method === 'initialize') {
    send({ jsonrpc: '2.0', id, result: { capabilities: {} } });
    continue;
  }
  if (msg.method === 'tools/list') {
    send({
      jsonrpc: '2.0',
      id,
      result: {
        tools: [
          {
            name: 'list_items',
            description: 'This tool enables users to retrieve repository information in order to list all items.',
            inputSchema: {
              type: 'object',
              properties: {
                page: { type: 'integer', description: 'This parameter should be provided for pagination.' }
              }
            }
          },
          {
            name: 'create_issue',
            description: 'Create an issue in a repository.',
            inputSchema: { type: 'object', properties: { title: { type: 'string' } } }
          }
        ]
      }
    });
    continue;
  }
  if (msg.method === 'tools/call') {
    const items = [];
    for (let i = 0; i < 60; i++) {
      items.push({
        very_long_common_key_name: i,
        another_repeated_field_name: i * 2,
        third_repeated_property_name: 'value-' + i,
      });
    }
    const payload = { items, status: 'ok' };
    send({
      jsonrpc: '2.0',
      id,
      result: {
        structuredContent: payload,
        content: [{ type: 'text', text: JSON.stringify(payload) }],
      }
    });
    continue;
  }
  if (id !== undefined) {
    send({ jsonrpc: '2.0', id, result: {} });
  }
}
`;
  fs.writeFileSync(file, script, 'utf-8');
  return { dir, file };
}

function createMessageReader(stream, getDebugText = () => '') {
  const rl = readline.createInterface({ input: stream, crlfDelay: Infinity });
  const queue = [];
  const waiters = [];

  rl.on('line', (line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    let msg;
    try {
      msg = JSON.parse(trimmed);
    } catch {
      return;
    }
    if (waiters.length > 0) {
      const waiter = waiters.shift();
      waiter(msg);
    } else {
      queue.push(msg);
    }
  });

  return {
    async next(timeoutMs = 5000, label = 'response') {
      if (queue.length > 0) {
        return queue.shift();
      }
      return await new Promise((resolve, reject) => {
        const waiter = (msg) => {
          clearTimeout(timer);
          resolve(msg);
        };
        const timer = setTimeout(() => {
          const idx = waiters.indexOf(waiter);
          if (idx >= 0) waiters.splice(idx, 1);
          reject(new Error(`Timed out waiting for ${label}\n${getDebugText()}`));
        }, timeoutMs);
        waiters.push(waiter);
      });
    },
    close() {
      rl.close();
    },
  };
}

function sendJson(child, msg) {
  child.stdin.write(`${JSON.stringify(msg)}\n`);
}

async function shutdownProxy(child) {
  child.stdin.end();
  await new Promise((resolve) => {
    const timer = setTimeout(() => {
      try { child.kill('SIGTERM'); } catch {}
      resolve();
    }, 3000);
    child.once('exit', () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

test('proxy integration: tools hash sync + caching + delta + result compression', async () => {
  const upstream = writeTempUpstream();
  const child = spawn(
    process.execPath,
    [
      CLI_PATH,
      'proxy',
      '--enable-result-compression',
      '--enable-delta-responses',
      '--enable-lazy-loading',
      '--lazy-mode',
      'minimal',
      '--enable-tools-hash-sync',
      '--enable-caching',
      '--stats',
      '--',
      process.execPath,
      upstream.file,
    ],
    { stdio: ['pipe', 'pipe', 'pipe'] }
  );

  let stderrText = '';
  child.stderr.on('data', (chunk) => {
    stderrText += chunk.toString('utf-8');
  });
  const reader = createMessageReader(
    child.stdout,
    () => `stderr:\n${stderrText}\nexitCode=${child.exitCode ?? 'running'}`
  );

  try {
    sendJson(child, {
      jsonrpc: '2.0',
      id: 1,
      method: 'initialize',
      params: {
        capabilities: {
          experimental: {
            ultra_lean_mcp_proxy: {
              tools_hash_sync: { version: 1 },
            },
          },
        },
      },
    });
    const initRsp = await reader.next(6000, 'initialize response');
    assert.equal(initRsp.id, 1);
    assert.equal(
      initRsp.result?.capabilities?.experimental?.ultra_lean_mcp_proxy?.tools_hash_sync?.version,
      1
    );

    sendJson(child, { jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} });
    const listRsp1 = await reader.next(6000, 'tools/list response #1');
    assert.equal(listRsp1.id, 2);
    assert.ok(Array.isArray(listRsp1.result?.tools));
    const toolsHash = listRsp1.result?._ultra_lean_mcp_proxy?.tools_hash_sync?.tools_hash;
    assert.equal(typeof toolsHash, 'string');

    sendJson(child, {
      jsonrpc: '2.0',
      id: 3,
      method: 'tools/list',
      params: {
        _ultra_lean_mcp_proxy: {
          tools_hash_sync: { if_none_match: toolsHash },
        },
      },
    });
    const listRsp2 = await reader.next(6000, 'tools/list response #2');
    assert.equal(listRsp2.id, 3);
    assert.deepEqual(listRsp2.result?.tools, []);
    assert.equal(listRsp2.result?._ultra_lean_mcp_proxy?.tools_hash_sync?.not_modified, true);

    sendJson(child, {
      jsonrpc: '2.0',
      id: 4,
      method: 'tools/call',
      params: { name: 'list_items', arguments: { page: 1 } },
    });
    const callRsp1 = await reader.next(6000, 'tools/call response #1');
    assert.equal(callRsp1.id, 4);
    assert.ok(callRsp1.result);

    sendJson(child, {
      jsonrpc: '2.0',
      id: 5,
      method: 'tools/call',
      params: { name: 'list_items', arguments: { page: 1 } },
    });
    const callRsp2 = await reader.next(6000, 'tools/call response #2');
    assert.equal(callRsp2.id, 5);
    assert.equal(callRsp2.result?.structuredContent?.delta?.unchanged, true);
  } finally {
    reader.close();
    await shutdownProxy(child);
    fs.rmSync(upstream.dir, { recursive: true, force: true });
  }
});

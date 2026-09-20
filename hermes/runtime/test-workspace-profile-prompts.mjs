import assert from 'node:assert/strict';
import { test } from 'node:test';
import { saveProfileSoul } from './workspace-profile-prompts.mjs';

test('Operations saves the real SOUL through the versioned, authenticated memory API', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({url, options});
    return Response.json(options?.method === 'POST' ? { success: true } : { content: 'original\n', version: 'v1' });
  };
  await saveProfileSoul('profiles/builder/SOUL.md', 'edited\n', 'original\n', fetchImpl);
  assert.match(calls[0].url, /\/api\/memory\/read\?path=/);
  assert.equal(calls[1].url, '/api/memory/write');
  assert.deepEqual(JSON.parse(calls[1].options.body), { path: 'profiles/builder/SOUL.md', content: 'edited\n', version: 'v1' });
  assert.equal(calls[1].options.headers['Content-Type'], 'application/json');
});

test('stale tabs, auth failures, invalid paths and server write errors never fall back to config.yaml', async () => {
  let writes = 0;
  const fetchImpl = async (_url, options) => {
    if (options?.method === 'POST') { writes++; return Response.json({}, {status: 409}); }
    return Response.json({content: 'newer', version: 'v2'});
  };
  await assert.rejects(saveProfileSoul('SOUL.md', 'edit', 'older', fetchImpl), /changed.*reload/i);
  assert.equal(writes, 0);
  await assert.rejects(saveProfileSoul('SOUL.md', 'edit', 'newer', fetchImpl), /409/);
  await assert.rejects(saveProfileSoul('../SOUL.md', 'edit', '', fetchImpl), /path/i);
  await assert.rejects(saveProfileSoul('SOUL.md', 'edit', '', async () => new Response('private error body', {status:401})), /401/);
});

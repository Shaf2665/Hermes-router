const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const vm = require('node:vm');
const esbuild = require('esbuild');
const moduleStub = { exports: {} };
vm.runInNewContext(esbuild.buildSync({ entryPoints: ['src/client.ts'], bundle: true,
  write: false, platform: 'node', format: 'cjs' }).outputFiles[0].text,
  { module: moduleStub, exports: moduleStub.exports, require, Buffer, console, setTimeout, clearTimeout });
const { RouterClient } = moduleStub.exports;

test('reasoning is omitted for server default and explicit Auto travels on discovery and chat', async () => {
  const seen = [];
  const server = http.createServer((req, res) => {
    seen.push(req.headers['x-hermes-reasoning']);
    req.resume();
    if (req.method === 'GET') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ schema_version: 1, revision: 'a'.repeat(64), context_window: 0,
        model: { id: 'hermes-router', context_window: 16000, max_input_tokens: 7808,
          max_output_tokens: 8192, capabilities: { tool_calling: true, image_input: false } } }));
    } else {
      res.setHeader('Content-Type', 'text/event-stream');
      res.end('data: [DONE]\n\n');
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const url = `http://127.0.0.1:${server.address().port}`;
    for (const level of ['server', 'auto', 'high']) {
      const client = new RouterClient(url, 'app-key', level);
      await client.getClientConfiguration();
      await client.streamChat({ model: 'hermes-router', messages: [], maxOutputTokens: 10,
        configRevision: 'a'.repeat(64), onText() {}, onToolCall() {},
        cancellation: { isCancellationRequested: false, onCancellationRequested: () => ({ dispose() {} }) } });
    }
    assert.deepEqual(seen, [undefined, undefined, 'auto', 'auto', 'high', 'high']);
    assert.equal(new RouterClient(url, 'key', 'low').sameConnection(new RouterClient(url, 'key', 'high')), false);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

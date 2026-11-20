const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const http = require('node:http');
const esbuild = require('esbuild');

function load(entry, vscode = {}) {
  const built = esbuild.buildSync({ entryPoints: [entry], bundle: true, write: false,
    platform: 'node', format: 'cjs', external: ['vscode'] }).outputFiles[0].text;
  const module = { exports: {} };
  vm.runInNewContext(built, { module, exports: module.exports, Buffer, console,
    require: name => name === 'vscode' ? vscode : require(name) });
  return module.exports;
}

function provider(config = {}, remote = { window: 0 }) {
  let sent;
  const vscode = {
    EventEmitter: class {
      listeners = []; event = fn => { this.listeners.push(fn); return { dispose() {} }; };
      fire() { this.listeners.forEach(fn => fn()); } dispose() { this.listeners = []; }
    },
    workspace: { getConfiguration: () => ({ get: (key, fallback) => config[key] ?? fallback }) },
    LanguageModelTextPart: class { constructor(value) { this.value = value; } },
    LanguageModelToolCallPart: class { constructor(callId, name, input) { Object.assign(this, { callId, name, input }); } },
    LanguageModelToolResultPart: class { constructor(callId, content) { Object.assign(this, { callId, content }); } },
    LanguageModelChatMessageRole: { User: 1, Assistant: 2 },
    LanguageModelChatToolMode: { Required: 2 },
  };
  const { HermesChatModelProvider } = load('src/lmProvider.ts', vscode);
  const model = new HermesChatModelProvider(() => ({
    getContextWindow: async () => { if (remote.error) throw new Error('offline'); return remote.window; },
    streamChat: async options => { sent = options; },
  }));
  return { model, vscode, sent: () => sent };
}

test('defaults preserve 40K display and changed settings update model information', async () => {
  const config = {}, { model } = provider(config);
  let changes = 0;
  model.onDidChangeLanguageModelChatInformation(() => changes++);
  const [initial] = await model.provideLanguageModelChatInformation();
  assert.equal(initial.maxInputTokens, 32000);
  assert.equal(initial.maxOutputTokens, 8192);
  config.contextWindow = 128000;
  model.refresh();
  assert.equal(changes, 1);
  const [updated] = await model.provideLanguageModelChatInformation();
  assert.equal(updated.maxInputTokens, 119808);
  model.dispose();
});

test('invalid combined settings fail with an actionable error', async () => {
  for (const config of [{ contextWindow: 8192 }, { contextWindow: 2.5 },
    { contextWindow: '128000' }, { maxOutputTokens: 0 }, { contextWindow: 2147483648 }]) {
    await assert.rejects(provider(config).model.provideLanguageModelChatInformation(), /hermesRouter.contextWindow/);
  }
});

test('dashboard updates change the advertised window, including lowering it to 16K', async () => {
  const remote = { window: 128000 }, { model } = provider({}, remote);
  const [large] = await model.provideLanguageModelChatInformation();
  assert.equal(large.maxInputTokens + large.maxOutputTokens, 128000);
  let changes = 0;
  model.onDidChangeLanguageModelChatInformation(() => changes++);
  remote.window = 16000;
  await model.syncContextWindow();
  assert.equal(changes, 1);
  const [small] = await model.provideLanguageModelChatInformation();
  assert.equal(small.maxInputTokens + small.maxOutputTokens, 16000);
  assert.equal(changes, 1);
  remote.error = true;
  const [offline] = await model.provideLanguageModelChatInformation();
  assert.equal(offline.maxInputTokens, small.maxInputTokens);
  remote.error = false;
  remote.window = 0;
  assert.equal((await model.provideLanguageModelChatInformation())[0].maxInputTokens, 32000);
});

test('an explicit client context can raise but never lower the dashboard minimum', async () => {
  const remote = { window: 128000 }, config = { contextWindow: 16000 };
  const { model } = provider(config, remote);
  assert.equal((await model.provideLanguageModelChatInformation())[0].maxInputTokens, 119808);
  config.contextWindow = 256000;
  assert.equal((await model.provideLanguageModelChatInformation())[0].maxInputTokens, 247808);
});

test('a response from the previous router cannot overwrite the new router setting', async () => {
  const { model } = provider();
  let resolve;
  model.getClient = () => ({ getContextWindow: () => new Promise(r => resolve = r) });
  const pending = model.syncContextWindow();
  model.refresh();
  resolve(128000);
  await pending;
  model.getClient = () => ({ getContextWindow: async () => { throw new Error('offline'); } });
  assert.equal((await model.provideLanguageModelChatInformation())[0].maxInputTokens, 32000);
});

test('HTTP context discovery uses the application key and supports old routers', async () => {
  const { RouterClient } = load('src/client.ts');
  let window = 128000;
  const server = http.createServer((req, res) => {
    assert.equal(req.url, '/v1/models');
    assert.equal(req.headers.authorization, 'Bearer app-test');
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ data: [{ id: 'hermes-router', owned_by: 'hermes-router', context_window: window }] }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const client = new RouterClient(`http://127.0.0.1:${server.address().port}/v1`, 'app-test');
    assert.equal(await client.getContextWindow(), 128000);
    window = undefined;
    assert.equal(await client.getContextWindow(), 0);
    window = '128K';
    await assert.rejects(client.getContextWindow(), /Invalid router context/);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('request uses selected context/output limits and counts tool calls/results', async () => {
  const { model, vscode, sent } = provider({ contextWindow: 128000 });
  const [info] = await model.provideLanguageModelChatInformation();
  const call = { role: 2, content: [new vscode.LanguageModelToolCallPart('c', 'read', { path: 'x'.repeat(2000) })] };
  const result = { role: 1, content: [new vscode.LanguageModelToolResultPart('c', [new vscode.LanguageModelTextPart('y'.repeat(3000))])] };
  assert.ok(await model.provideTokenCount(info, call) > 500);
  assert.ok(await model.provideTokenCount(info, result) > 750);
  await model.provideLanguageModelChatResponse(info, [call, result], { tools: [{ name: 'read' }] },
    { report() {} }, { onCancellationRequested() {} });
  assert.equal(sent().contextWindow, 128000);
  assert.equal(sent().maxOutputTokens, 8192);
  assert.equal(sent().messages[1].role, 'tool');
});

test('HTTP client sends context as a router-only header and the reply budget in the body', async () => {
  const { RouterClient } = load('src/client.ts');
  let received;
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      received = { headers: req.headers, body: JSON.parse(body) };
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.end('data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n');
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const client = new RouterClient(`http://127.0.0.1:${server.address().port}/v1`, 'app-test');
    let text = '';
    await client.streamChat({ messages: [], contextWindow: 128000, maxOutputTokens: 8192,
      onText: delta => text += delta });
    assert.equal(received.headers['x-hermes-context-window'], '128000');
    assert.equal(received.headers.authorization, 'Bearer app-test');
    assert.equal(received.body.max_tokens, 8192);
    assert.equal(received.body.context_window, undefined);
    assert.equal(text, 'ok');
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

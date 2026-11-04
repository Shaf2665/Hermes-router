const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const esbuild = require('esbuild');
const http = require('node:http');

class Emitter {
  callbacks = new Set();
  event = fn => { this.callbacks.add(fn); return { dispose: () => this.callbacks.delete(fn) }; };
  fire() { for (const fn of this.callbacks) fn(); }
  dispose() { this.callbacks.clear(); }
}
class TextPart { constructor(value) { this.value = value; } }
class ToolCallPart { constructor(callId, name, input) { Object.assign(this, { callId, name, input }); } }
class ToolResultPart { constructor(callId, content) { Object.assign(this, { callId, content }); } }
const vscode = {
  EventEmitter: Emitter, LanguageModelTextPart: TextPart,
  LanguageModelToolCallPart: ToolCallPart, LanguageModelToolResultPart: ToolResultPart,
  LanguageModelChatMessageRole: { User: 1, Assistant: 2 }, LanguageModelChatToolMode: { Required: 1 },
};
function load(path) {
  const built = esbuild.buildSync({ entryPoints: [path], bundle: true, write: false,
    platform: 'node', format: 'cjs', external: ['vscode'] }).outputFiles[0].text;
  const module = { exports: {} };
  vm.runInNewContext(built, { module, exports: module.exports, Buffer, console,
    require: name => name === 'vscode' ? vscode : require(name) });
  return module.exports;
}
const { HermesChatModelProvider } = load('src/lmProvider.ts');
const { RouterClient } = load('src/client.ts');
const { parseClientConfiguration } = load('src/contextConfig.ts');
const cancel = { isCancellationRequested: false, onCancellationRequested: () => ({ dispose() {} }) };
const config = (context = 256000, revision = 'a', output = 8192) => ({
  schema_version: 1, revision: revision.repeat(64), context_window: context,
  model: { id: 'custom-router', context_window: context, max_input_tokens: context - output,
    max_output_tokens: output, capabilities: { tool_calling: true, image_input: false } },
});
function fakeClient(id, fetch) {
  return { id, sameConnection: other => other.id === id, getClientConfiguration: fetch,
    streamChat: async () => {} };
}
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

test('model picker follows increases, decreases, Auto and unchanged polls', async () => {
  let current = config(), events = 0;
  const client = fakeClient('one', async () => current);
  const provider = new HermesChatModelProvider(() => client);
  provider.onDidChangeLanguageModelChatInformation(() => events++);
  let [model] = await provider.provideLanguageModelChatInformation({}, cancel);
  assert.equal(model.maxInputTokens, 247808);
  assert.equal(model.maxOutputTokens, 8192);
  await provider.refreshConfiguration(); assert.equal(events, 1);
  current = config(512000, 'b');
  [model] = await provider.provideLanguageModelChatInformation({}, cancel);
  assert.equal(model.maxInputTokens, 503808);
  current = config(16000, 'c');
  [model] = await provider.provideLanguageModelChatInformation({}, cancel);
  assert.equal(model.maxInputTokens, 7808);
  current = { ...config(128000, 'd'), context_window: 0 };
  [model] = await provider.provideLanguageModelChatInformation({}, cancel);
  assert.equal(model.maxInputTokens, 119808);
  assert.equal(events, 4);
  provider.dispose();
});

test('no eligible models removes the picker entry; recovery re-advertises it', async () => {
  let current = { ...config(), model: null, reason: 'No confirmed capacities' };
  const client = fakeClient('one', async () => current);
  const provider = new HermesChatModelProvider(() => client);
  assert.equal((await provider.provideLanguageModelChatInformation({}, cancel)).length, 0);
  current = config(256000, 'b');
  assert.equal((await provider.provideLanguageModelChatInformation({}, cancel)).length, 1);
  provider.dispose();
});

test('preflight applies changed limits to an existing conversation before sending', async () => {
  let current = config(), sent;
  const client = fakeClient('one', async () => current);
  client.streamChat = async opts => sent = opts;
  const provider = new HermesChatModelProvider(() => client);
  const [model] = await provider.provideLanguageModelChatInformation({}, cancel);
  current = config(16000, 'b', 2000);
  await provider.provideLanguageModelChatResponse(model, [], {}, { report() {} }, cancel);
  assert.equal(sent.maxOutputTokens, 2000);
  assert.equal(sent.configRevision, 'b'.repeat(64));
  sent = undefined;
  await assert.rejects(provider.provideLanguageModelChatResponse(model,
    [{ role: 1, content: [new TextPart('x'.repeat(15000))] }], {}, { report() {} }, cancel), /input budget/);
  assert.equal(sent, undefined);
  provider.dispose();
});

test('offline or revoked keys fail closed; reconnect refreshes metadata', async () => {
  let failure = false, sent = 0, events = 0;
  const client = fakeClient('one', async () => { if (failure) throw Error('unauthorized'); return config(); });
  client.streamChat = async () => sent++;
  const provider = new HermesChatModelProvider(() => client);
  provider.onDidChangeLanguageModelChatInformation(() => events++);
  const [model] = await provider.provideLanguageModelChatInformation({}, cancel);
  failure = true;
  await assert.rejects(provider.provideLanguageModelChatResponse(model, [], {}, { report() {} }, cancel), /unauthorized/);
  assert.equal(sent, 0); assert.equal(events, 2);
  failure = false;
  await provider.refreshConfiguration(); assert.equal(events, 3);
  provider.dispose();
});

test('late responses cannot restore limits from a different router or key', async () => {
  const slow = deferred();
  let client = fakeClient('old-url-key', () => slow.promise);
  const provider = new HermesChatModelProvider(() => client);
  const old = provider.refreshConfiguration();
  const rejected = assert.rejects(old, /connection changed/);
  client = fakeClient('new-url-key', async () => config(16000, 'b'));
  provider.invalidate();
  const [newModel] = await provider.provideLanguageModelChatInformation({}, cancel);
  slow.resolve(config(1000000)); await rejected;
  const [model] = await provider.provideLanguageModelChatInformation({}, cancel);
  assert.equal(model.maxInputTokens, newModel.maxInputTokens);
  provider.dispose();
});

test('concurrent refreshes share a request and disposed providers ignore its response', async () => {
  const slow = deferred(); let calls = 0, events = 0;
  const client = fakeClient('one', () => { calls++; return slow.promise; });
  const provider = new HermesChatModelProvider(() => client);
  provider.onDidChangeLanguageModelChatInformation(() => events++);
  const p1 = provider.refreshConfiguration(), p2 = provider.refreshConfiguration();
  const rejected1 = assert.rejects(p1), rejected2 = assert.rejects(p2);
  assert.equal(calls, 1);
  provider.dispose(); slow.resolve(config());
  await Promise.all([rejected1, rejected2]); assert.equal(events, 0);
});

test('chat carries the authoritative model, output budget and revision; tools count toward input', async () => {
  let sent;
  const client = fakeClient('one', async () => config());
  client.streamChat = async opts => { sent = opts; };
  const provider = new HermesChatModelProvider(() => client);
  const [model] = await provider.provideLanguageModelChatInformation({}, cancel);
  const messages = [{ role: 1, content: [new TextPart('Hello')] }];
  await provider.provideLanguageModelChatResponse(model, messages, {}, { report() {} }, cancel);
  assert.equal(sent.model, 'custom-router'); assert.equal(sent.maxOutputTokens, 8192);
  assert.equal(sent.configRevision, 'a'.repeat(64));
  sent = undefined;
  await assert.rejects(provider.provideLanguageModelChatResponse(model, messages,
    { tools: [{ name: 'test', description: 'அ'.repeat(100000), inputSchema: {} }] }, { report() {} }, cancel), /input budget/);
  assert.equal(sent, undefined);
  const largeTool = { role: 2, content: [new ToolCallPart('id', 'test', { text: 'அ'.repeat(100) })] };
  assert.ok(await provider.provideTokenCount(model, largeTool, cancel) > 300);
  provider.dispose();
});

test('invalid metadata never becomes a model limit', () => {
  for (const bad of [null, {}, { ...config(), revision: 'bad' }, { ...config(), schema_version: 2 },
    ...[true, -1, 0, 1.5, '1000', 1e30].map(n => ({ ...config(), model: { ...config().model, max_input_tokens: n } }))]) {
    assert.throws(() => parseClientConfiguration(bad));
  }
  assert.equal(parseClientConfiguration(config()).model.context_window, 256000);
});

test('HTTP client uses application authentication and sends context contract on the wire', async t => {
  const seen = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      seen.push({ url: req.url, headers: req.headers, body });
      if (req.url === '/v1/client-config') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(config())); }
      else { res.setHeader('Content-Type', 'text/event-stream'); res.end('data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n'); }
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const client = new RouterClient(`http://127.0.0.1:${server.address().port}/v1`, 'app-only-key');
  const conf = await client.getClientConfiguration(); let text = '';
  await client.streamChat({ model: conf.model.id, messages: [], maxOutputTokens: conf.model.max_output_tokens,
    configRevision: conf.revision, onText: delta => text += delta });
  assert.equal(text, 'OK');
  assert.equal(seen[0].headers.authorization, 'Bearer app-only-key');
  assert.equal(seen[1].headers['x-hermes-config-revision'], conf.revision);
  assert.equal(JSON.parse(seen[1].body).max_tokens, 8192);
  assert.equal(JSON.parse(seen[1].body).model, 'custom-router');
});

test('cancellation during preflight sends nothing; streaming cancellation subscriptions are disposed', async () => {
  const slow = deferred(); let sent = 0;
  const client = fakeClient('one', () => slow.promise);
  client.streamChat = async () => sent++;
  const provider = new HermesChatModelProvider(() => client);
  const token = { ...cancel };
  const response = provider.provideLanguageModelChatResponse({}, [], {}, { report() {} }, token);
  token.isCancellationRequested = true;
  slow.resolve(config()); await response;
  assert.equal(sent, 0);
  token.isCancellationRequested = false;
  let disposed = false;
  token.onCancellationRequested = () => ({ dispose: () => disposed = true });
  client.getClientConfiguration = async () => config();
  client.streamChat = async opts => opts.onAbort(() => {});
  const [model] = await provider.provideLanguageModelChatInformation({}, cancel);
  await provider.provideLanguageModelChatResponse(model, [], {}, { report() {} }, token);
  assert.equal(disposed, true);
  provider.dispose();
});

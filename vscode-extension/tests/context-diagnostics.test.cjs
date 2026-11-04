const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const esbuild = require('esbuild');

const built = esbuild.buildSync({ entryPoints: ['src/extension.ts'], bundle: true,
  write: false, platform: 'node', format: 'cjs', external: ['vscode'] }).outputFiles[0].text;

test('context diagnostics show the real budget and explain rejected application keys', async () => {
  const lines = [], notices = [], commands = new Map();
  const disposable = { dispose() {} };
  let code = 200, model;
  const config = { baseUrl: 'https://router.example/v1', apiKey: 'application-secret' };
  const vscode = {
    EventEmitter: class { event = () => disposable; fire() {} dispose() {} },
    StatusBarAlignment: { Left: 1 }, ThemeColor: class {},
    window: {
      createOutputChannel: () => ({ ...disposable, show() {}, appendLine: text => lines.push(text) }),
      createStatusBarItem: () => ({ ...disposable, show() {} }),
      registerWebviewViewProvider: () => disposable,
      showInformationMessage: text => notices.push(text),
      showErrorMessage: text => notices.push(text),
    },
    workspace: { getConfiguration: () => ({ get: (key, fallback) => config[key] ?? fallback }),
      onDidChangeConfiguration: () => disposable },
    commands: { registerCommand: (name, fn) => { commands.set(name, fn); return disposable; } },
    lm: { registerLanguageModelChatProvider: (_, provider) => { model = provider; return disposable; } },
  };
  const transport = { request: (url, options, callback) => {
    const req = new EventEmitter(); req.write = () => {};
    req.end = () => queueMicrotask(() => {
      const response = new EventEmitter();
      const context = url.pathname === '/v1/client-config';
      response.statusCode = context ? code : 200;
      if (context) assert.equal(options.headers.Authorization, 'Bearer application-secret');
      callback(response);
      response.emit('data', JSON.stringify(context ? {
        schema_version: 1, revision: 'a'.repeat(64), context_window: 256000,
        model: { id: 'hermes-router', context_window: 256000, max_input_tokens: 247808,
          max_output_tokens: 8192, capabilities: { tool_calling: true, image_input: false } },
      } : { status: 'ok', providers: [] }));
      response.emit('end');
    });
    return req;
  }};
  const module = { exports: {} };
  vm.runInNewContext(built, { module, exports: module.exports, Buffer, console,
    setInterval: () => 1, clearInterval() {},
    require: name => name === 'vscode' ? vscode : ['http', 'https'].includes(name) ? transport : require(name) });
  const context = { subscriptions: [], secrets: { get: async () => undefined } };
  module.exports.activate(context);
  await commands.get('hermesRouter.checkContextSync')();
  assert.ok(lines.some(s => s.includes('256,000 total tokens (247,808 input + 8,192 output)')));
  code = 401;
  await commands.get('hermesRouter.checkContextSync')();
  assert.ok(notices.some(s => s.includes('active application key from dashboard Connect')));
  await assert.rejects(model.provideLanguageModelChatInformation({}, { isCancellationRequested: false }), /unauthorized/);
  assert.ok(!lines.join('\n').includes(config.apiKey));
  config.baseUrl = 'not-a-url?secret=embedded-secret';
  model.invalidate();
  await commands.get('hermesRouter.checkContextSync')();
  assert.ok(notices.some(s => s.includes('Invalid router URL')));
  assert.ok(!lines.join('\n').includes('embedded-secret'));
  for (const item of context.subscriptions) item.dispose();
});

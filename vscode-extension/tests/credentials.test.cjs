const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const esbuild = require('esbuild');

// Exercise activation with VS Code's public SecretStorage contract, no real host or network.
test('owner credentials are scoped SecretStorage; legacy app key remains for chat', async () => {
  const commands = new Map(), secrets = new Map(), secretReads = [];
  const config = { baseUrl: 'https://router.example/v1', apiKey: 'existing-app-key' };
  let input = 'owner-secret', dashboard, model;
  const disposable = { dispose() {} };
  const vscode = {
    StatusBarAlignment: { Left: 1 },
    ThemeColor: class {},
    window: {
      createOutputChannel: () => disposable,
      createStatusBarItem: () => ({ ...disposable, show() {} }),
      registerWebviewViewProvider: (_, provider) => { dashboard = provider; return disposable; },
      showInputBox: async (options) => { assert.equal(options.password, true); return input; },
    },
    workspace: {
      getConfiguration: () => ({ get: (key, fallback) => config[key] ?? fallback }),
      onDidChangeConfiguration: () => disposable,
    },
    commands: { registerCommand: (name, fn) => { commands.set(name, fn); return disposable; } },
    lm: { registerLanguageModelChatProvider: (_, provider) => { model = provider; return disposable; } },
  };
  const built = esbuild.buildSync({
    entryPoints: ['src/extension.ts'], bundle: true, write: false, platform: 'node', format: 'cjs',
    external: ['vscode'], plugins: [],
  }).outputFiles[0].text;
  const module = { exports: {} };
  const { EventEmitter } = require('node:events');
  const transport = { request: (_url, _options, callback) => {
    const request = new EventEmitter();
    request.end = () => {
      const response = new EventEmitter(); response.statusCode = 200;
      callback(response);
      response.emit('data', '{"status":"ok","providers":[]}'); response.emit('end');
    };
    request.write = () => {};
    return request;
  }};
  vm.runInNewContext(built, {
    module, exports: module.exports,
    require: (name) => name === 'vscode' ? vscode : ['http','https'].includes(name) ? transport : require(name),
    setInterval: () => 1, clearInterval() {}, Buffer, console,
  });
  const context = {
    subscriptions: [],
    secrets: {
      get: async key => { secretReads.push(key); return secrets.get(key); },
      store: async (key, value) => { secrets.set(key, value); },
      delete: async key => { secrets.delete(key); },
    },
  };
  module.exports.activate(context);
  // The model client retains the existing application key, without consulting owner storage.
  assert.equal(model.getClient().apiKey, 'existing-app-key');
  await assert.rejects(dashboard.getClient(), /Set Administrator Credential/);
  // The isolated transport handles the initial optional health fallback; later refreshes need no network.
  dashboard.refresh = async () => ({ providers: {} });
  await commands.get('hermesRouter.setAdminCredential')();
  assert.equal((await dashboard.getClient()).apiKey, 'owner-secret');
  assert.equal(secrets.get('hermesRouter.admin:https://router.example/dashboard'), 'owner-secret');
  assert.equal(config.apiKey, 'existing-app-key');
  config.baseUrl = 'https://other-router.example';
  await assert.rejects(dashboard.getClient(), /Set Administrator Credential/);
  config.baseUrl = 'https://router.example/v1';
  input = '';
  await commands.get('hermesRouter.setAdminCredential')();
  assert.equal(secrets.size, 0);
  assert.equal(model.getClient().apiKey, 'existing-app-key');
  for (const entry of context.subscriptions) entry.dispose?.();
});

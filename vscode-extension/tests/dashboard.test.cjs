const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const esbuild = require('esbuild');

const built = esbuild.buildSync({
  entryPoints: ['src/dashboard.ts'], bundle: true, write: false,
  platform: 'node', format: 'cjs', external: ['vscode'],
}).outputFiles[0].text;

function panel(baseUrl) {
  const module = { exports: {} }, commands = [];
  vm.runInNewContext(built, {
    module, exports: module.exports,
    require: name => name === 'vscode' ? {
      workspace: { getConfiguration: () => ({ get: (key, fallback) => key === 'baseUrl' ? baseUrl : fallback }) },
      commands: { executeCommand: command => commands.push(command) },
    } : require(name),
  });
  return { provider: new module.exports.DashboardProvider(async () => { throw new Error('Set Administrator Credential'); }), commands };
}

for (const base of ['http://localhost:8319', 'https://router.example/v1']) {
  test(`dashboard script renders and buttons work: ${base}`, () => {
    const { provider } = panel(base);
    const source = provider.shell().match(/<script>([\s\S]*?)<\/script>/)[1];
    const handlers = {}, sent = [], content = { innerHTML: '' };
    new vm.Script(source).runInNewContext({
      acquireVsCodeApi: () => ({ postMessage: msg => sent.push(msg) }),
      window: { addEventListener: (name, fn) => { handlers[name] = fn; } },
      document: {
        getElementById: () => content,
        addEventListener: (name, fn) => { handlers[name] = fn; },
      },
    });
    assert.equal(sent[0].type, 'refresh');
    handlers.message({ data: { type: 'error', message: '<invalid credential>' } });
    assert.match(content.innerHTML, /&lt;invalid credential&gt;/);
    assert.match(content.innerHTML, /Set administrator credential/);
    handlers.click({ target: { closest: () => ({ dataset: { command: 'hermesRouter.setAdminCredential' } }) } });
    assert.equal(sent.at(-1).command, 'hermesRouter.setAdminCredential');
    handlers.message({ data: { type: 'status', status: {
      providers: { '<provider>': { model: 'test-model', keys: [{ status: 'ready' }], stats: {} } },
    } } });
    assert.match(content.innerHTML, /Hermes is ready/);
    assert.match(content.innerHTML, /&lt;provider&gt;/);
    assert.match(content.innerHTML, /test-model/);
    handlers.click({ target: { closest: () => ({ dataset: { action: 'refresh' } }) } });
    assert.equal(sent.at(-1).type, 'refresh');
    assert.doesNotMatch(content.innerHTML, /onclick=/);
  });
}

test('missing owner credential is displayed, not left loading; commands are restricted', async () => {
  const { provider, commands } = panel('https://router.example');
  const sent = [];
  let receive;
  const view = { webview: {
    onDidReceiveMessage: fn => { receive = fn; },
    postMessage: msg => { sent.push(msg); return Promise.resolve(true); },
  } };
  provider.resolveWebviewView(view);
  await provider.refresh();
  assert.equal(sent.at(-1).type, 'error');
  assert.match(sent.at(-1).message, /Set Administrator Credential/);
  receive({ type: 'command', command: 'hermesRouter.setAdminCredential' });
  receive({ type: 'command', command: 'workbench.action.terminal.new' });
  assert.deepEqual(commands, ['hermesRouter.setAdminCredential']);
});

import * as vscode from "vscode";
import { RouterClient, RouterStatus } from "./client";
import { isDocker, isLocal } from "./cli";

export class DashboardProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "hermesRouter.dashboard";
  private view?: vscode.WebviewView;

  constructor(private getClient: () => Promise<RouterClient>) {}

  resolveWebviewView(view: vscode.WebviewView) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = this.shell();
    view.webview.onDidReceiveMessage((msg) => {
      if (msg?.type === "refresh") {
        void this.refresh();
      } else if (msg?.type === "command" && typeof msg.command === "string") {
        void vscode.commands.executeCommand(msg.command);
      }
    });
    void this.refresh();
  }

  /** Fetch /v1/status and push it to the webview. */
  async refresh(): Promise<RouterStatus | null> {
    if (!this.view) return null;
    try {
      const status = await (await this.getClient()).getStatus();
      this.view.webview.postMessage({ type: "status", status });
      return status;
    } catch (e: any) {
      this.view.webview.postMessage({ type: "error", message: e?.message || String(e) });
      return null;
    }
  }

  private shell(): string {
    const canRunDoctor = isDocker() || isLocal();
    const doctorButton = canRunDoctor
      ? `<button class="secondary" onclick="cmd('hermesRouter.doctor')">Run doctor</button>`
      : `<button class="secondary" onclick="cmd('hermesRouter.doctor')">Doctor info</button>`;
    // Compact control center. The browser dashboard remains the single place for
    // configuration writes; this panel makes the current state understandable in
    // VS Code without forcing users to read a dense provider table first.
    return /* html */ `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<style>
  :root { --ok: var(--vscode-testing-iconPassed, #3fb950); --bad: var(--vscode-testing-iconFailed, #f85149); --warn: var(--vscode-editorWarning-foreground, #d29922); }
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); padding: 10px; }
  button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border:none; padding:6px 9px; border-radius:3px; cursor:pointer; font-weight:600; }
  button.secondary { background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); }
  button:hover { opacity:.9; }
  .stack { display:grid; gap:10px; }
  .hero { border:1px solid var(--vscode-panel-border); border-radius:6px; padding:12px; background: var(--vscode-sideBarSectionHeader-background); }
  .state { display:inline-block; font-size:11px; padding:2px 7px; border-radius:999px; margin-bottom:8px; background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); }
  .state.ok { color: var(--ok); }
  .state.warn { color: var(--warn); }
  .state.bad { color: var(--bad); }
  h2 { font-size:15px; margin:0 0 4px; }
  h3 { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color: var(--vscode-descriptionForeground); margin:2px 0 6px; }
  p { margin:0; color: var(--vscode-descriptionForeground); line-height:1.45; }
  .actions { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .metrics { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .metric, .card { border:1px solid var(--vscode-panel-border); border-radius:6px; padding:9px; }
  .label { color: var(--vscode-descriptionForeground); font-size:11px; margin-bottom:3px; }
  .value { font-size:17px; font-weight:700; font-variant-numeric:tabular-nums; }
  .list { display:grid; gap:6px; }
  .provider { border:1px solid var(--vscode-panel-border); border-radius:5px; padding:8px; }
  .provider.bad { border-color: color-mix(in srgb, var(--bad) 45%, var(--vscode-panel-border)); }
  .provider.warn { border-color: color-mix(in srgb, var(--warn) 45%, var(--vscode-panel-border)); }
  .row { display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .name { font-weight:700; }
  .model, .muted { color: var(--vscode-descriptionForeground); font-size:11px; line-height:1.4; }
  .dot { width:7px; height:7px; border-radius:50%; display:inline-block; margin-right:5px; background: var(--ok); }
  .dot.warn { background: var(--warn); }
  .dot.bad { background: var(--bad); }
  .pill { font-size:10px; padding:1px 6px; border-radius:999px; background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); white-space:nowrap; }
  .err { color: var(--bad); padding:8px 0; }
</style></head>
<body>
  <div id="content" class="muted">Loading...</div>
<script>
  const vscode = acquireVsCodeApi();
  function send(type){ vscode.postMessage({type}); }
  function cmd(command){ vscode.postMessage({type:'command', command}); }
  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
  function num(n){ return n == null ? '0' : Number(n).toLocaleString(); }
  function tok(n){ if(!n) return '0'; if(n>=1e6) return (n/1e6).toFixed(1)+'M'; if(n>=1e3) return (n/1e3).toFixed(1)+'K'; return String(n); }
  function ms(n){ return n == null ? '-' : (n >= 1000 ? (n/1000).toFixed(1)+'s' : Math.round(n)+'ms'); }
  function usd(n){ return !n ? '$0' : (n < 0.0001 ? '<$0.0001' : '$' + n.toFixed(4)); }

  window.addEventListener('message', (ev) => {
    const m = ev.data;
    const el = document.getElementById('content');
    if (m.type === 'error') {
      el.innerHTML = '<div class="err">⚠ ' + esc(m.message) + '</div>' +
        '<div class="muted">Check the router URL and run Set Administrator Credential for owner controls. Chat continues to use your application key setting.</div>' +
        '<div class="actions" style="margin-top:10px"><button onclick="send(\\'refresh\\')">Refresh</button><button class="secondary" onclick="cmd(\\'hermesRouter.openWebDashboard\\')">Open web</button><button onclick="cmd(\\'hermesRouter.setAdminCredential\\')">Set administrator credential</button></div>';
      return;
    }
    if (m.type !== 'status') return;
    const s = m.status || {};
    const provs = s.providers || {};
    const names = Object.keys(provs).sort();
    const cache = s.cache || {};
    const mode = (s.rotation && s.rotation.mode) || '—';
    let totalTokens = 0;
    let totalCost = 0;
    let totalRequests = 0;
    let totalErrors = 0;
    let readyKeys = 0;
    let totalKeys = 0;
    let openBreakers = 0;

    const items = names.map(n => {
      const p = provs[n] || {};
      totalTokens += (p.tokens || 0);
      totalCost += (p.cost_usd || 0);
      totalRequests += p.stats?.total_requests || 0;
      totalErrors += p.stats?.errors || 0;
      const keys = (p.keys||[]);
      totalKeys += keys.length;
      readyKeys += keys.filter(k=>k.status==='ready').length;
      if (p.breaker?.open) openBreakers++;
      const ready = keys.filter(k=>k.status==='ready').length;
      const req = p.stats?.total_requests || 0;
      const err = p.stats?.errors || 0;
      const errPct = req ? err / req * 100 : 0;
      const cls = p.breaker?.open || errPct > 25 ? 'bad' : errPct > 5 ? 'warn' : '';
      const dot = cls ? '<span class="dot '+cls+'"></span>' : '<span class="dot"></span>';
      const status = p.breaker?.open ? 'paused' : errPct > 25 ? 'check' : errPct > 5 ? 'watch' : 'ready';
      return { name:n, score:(cls==='bad'?2:cls==='warn'?1:0), html:
        '<div class="provider '+cls+'"><div class="row"><span class="name">'+dot+esc(n)+'</span><span class="pill">'+status+'</span></div>' +
        '<div class="model">'+esc(p.model||'no model')+'</div>' +
        '<div class="row muted"><span>'+ready+' ready key'+(ready===1?'':'s')+'</span><span>'+ms(p.stats?.avg_latency_ms)+'</span></div></div>'
      };
    }).sort((a,b)=>b.score-a.score || a.name.localeCompare(b.name));

    const sem = cache.semantic || {};
    const errRate = totalRequests ? totalErrors / totalRequests * 100 : 0;
    const stateCls = !totalKeys || openBreakers || errRate > 25 ? 'bad' : errRate > 5 ? 'warn' : 'ok';
    const stateText = !totalKeys ? 'Needs a key' : stateCls === 'bad' ? 'Needs attention' : stateCls === 'warn' ? 'Running with warnings' : 'Ready';
    const headline = !totalKeys ? 'Add a provider key to start' : stateCls === 'ok' ? 'Hermes is ready' : 'Hermes is still routing';
    const detail = !totalKeys ? 'Open the web dashboard and add at least one provider API key.' :
      stateCls === 'ok' ? 'Provider pool looks healthy.' :
      'Fallback is active. Check providers with warnings when you have time.';

    el.innerHTML = '<div class="stack">' +
      '<section class="hero"><span class="state '+stateCls+'">'+stateText+'</span><h2>'+headline+'</h2><p>'+detail+'</p></section>' +
      '<div class="actions"><button onclick="cmd(\\'hermesRouter.openWebDashboard\\')">Open web dashboard</button><button class="secondary" onclick="cmd(\\'hermesRouter.restart\\')">Restart router</button></div>' +
      '<div class="metrics"><div class="metric"><div class="label">Ready keys</div><div class="value">'+readyKeys+'/'+totalKeys+'</div></div>' +
      '<div class="metric"><div class="label">Errors</div><div class="value">'+errRate.toFixed(1)+'%</div></div>' +
      '<div class="metric"><div class="label">Tokens</div><div class="value">'+tok(totalTokens)+'</div></div>' +
      '<div class="metric"><div class="label">Spend</div><div class="value">'+usd(totalCost)+'</div></div></div>' +
      '<div class="card"><h3>Router</h3><p>Rotation: <span class="pill">'+esc(mode)+'</span> Cache: <span class="pill">'+(cache.enabled ? Math.round((cache.hit_rate||0)*100)+'%' : 'off')+'</span>'+(sem.enabled ? ' <span class="pill">semantic</span>' : '')+'</p></div>' +
      '<div><h3>Providers needing attention first</h3><div class="list">'+(items.map(i=>i.html).join('') || '<p>No providers configured.</p>')+'</div></div>' +
      '<div class="actions"><button class="secondary" onclick="send(\\'refresh\\')">Refresh</button>${doctorButton}</div>' +
      '</div>';
  });

  // Pull data as soon as this script is ready. resolveWebviewView also pushes an
  // initial status, but that message can race the listener above (and be lost),
  // leaving the panel stuck on "Loading…". Requesting a refresh here guarantees we
  // fetch once we're actually listening — the handler calls dashboard.refresh().
  send('refresh');
</script>
</body></html>`;
  }
}

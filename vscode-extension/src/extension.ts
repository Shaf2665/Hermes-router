import * as vscode from "vscode";
import { RouterClient } from "./client";
import { StatusBar } from "./statusBar";
import { DashboardProvider } from "./dashboard";
import { HermesChatModelProvider } from "./lmProvider";
import { runHr, runHrTerminal, isDocker, isLocal } from "./cli";

function makeClient(): RouterClient {
  const cfg = vscode.workspace.getConfiguration("hermesRouter");
  return new RouterClient(
    cfg.get<string>("baseUrl", "http://localhost:8319"),
    cfg.get<string>("apiKey", "")
  );
}

export function activate(context: vscode.ExtensionContext) {
  const out = vscode.window.createOutputChannel("hermes-router");
  const statusBar = new StatusBar();
  const dashboard = new DashboardProvider(makeClient);

  context.subscriptions.push(
    out,
    statusBar,
    vscode.window.registerWebviewViewProvider(DashboardProvider.viewType, dashboard),
    // Register hermes-router as a Language Model provider so it shows up in
    // Copilot Chat's model picker and any vscode.lm consumer can select it.
    vscode.lm.registerLanguageModelChatProvider(
      "hermes-router",
      new HermesChatModelProvider(makeClient)
    )
  );

  // ── Shared refresh: update the dashboard panel AND the status bar ────────────
  // Update the webview first and unconditionally — dashboard.refresh() always posts
  // a `status` or `error` message to it, so the panel can never get stuck on
  // "Loading…". The /health probe only feeds the status bar; a health blip must not
  // block the panel from rendering (that was the old bug).
  const refresh = async () => {
    const status = await dashboard.refresh();
    if (status) {
      statusBar.setHealthy(status);
      return;
    }
    // getStatus failed (unreachable or bad key). Fall back to /health so the status
    // bar can distinguish "router down" from "router up but /v1/status rejected".
    try {
      await makeClient().getHealth();
      statusBar.setUnknown();
    } catch (e: any) {
      statusBar.setUnreachable(e?.message || "no response");
    }
  };

  // ── Commands ─────────────────────────────────────────────────────────────────
  const reg = (id: string, fn: (...a: any[]) => any) =>
    context.subscriptions.push(vscode.commands.registerCommand(id, fn));

  reg("hermesRouter.openDashboard", async () => {
    await vscode.commands.executeCommand("hermesRouter.dashboard.focus");
    await refresh();
  });
  // Open the full browser dashboard (served by the router at /dashboard). The
  // in-editor webview is a compact view; the web one has the live request log,
  // per-key usage, and richer charts.
  reg("hermesRouter.openWebDashboard", async () => {
    const url = makeClient().dashboardUrl();
    await vscode.env.openExternal(vscode.Uri.parse(url));
  });
  reg("hermesRouter.refresh", refresh);

  reg("hermesRouter.restart", async () => {
    if (!isDocker() && !isLocal()) {
      try {
        await makeClient().restart();
        vscode.window.showInformationMessage("Restart requested. Hermes Router will reconnect in a few seconds.");
      } catch (e: any) {
        vscode.window.showWarningMessage(e?.message || "Could not restart the remote router.");
      }
    } else {
      await runHr(out, ["restart"]);
    }
    await refresh();
  });
  reg("hermesRouter.doctor", async () => {
    if (!isDocker() && !isLocal()) {
      const pick = await vscode.window.showInformationMessage(
        "Doctor runs on the machine or Docker container that hosts Hermes Router. This extension is connected to a remote URL.",
        "Open web dashboard"
      );
      if (pick === "Open web dashboard") {
        await vscode.env.openExternal(vscode.Uri.parse(makeClient().dashboardUrl()));
      }
      return;
    }
    await runHr(out, ["doctor"]);
  });
  reg("hermesRouter.update", async () => {
    if (isDocker()) {
      vscode.window.showInformationMessage(
        "To update a Docker router, pull a newer image and recreate the container — " +
          "e.g. `docker pull shafiq735/hermes-router:cli` then re-run it. `hr update` " +
          "doesn't apply inside a container."
      );
      return;
    }
    if (!isLocal()) {
      vscode.window.showInformationMessage(
        "Update must run on the machine that hosts Hermes Router. This extension is connected to a remote URL."
      );
      return;
    }
    await runHr(out, ["update"]);
    await refresh();
  });

  // Codex import stays here: it reads a local ~/.codex OAuth login from this
  // machine, which isn't something a paste-a-key web form can do. Everything
  // else config-related (API keys, provider models, add-ons, rotation mode)
  // lives in the web dashboard now — see hermesRouter.openWebDashboard above.
  reg("hermesRouter.importCodex", () => {
    if (isDocker()) {
      vscode.window.showInformationMessage(
        "Codex import reads your ChatGPT login (~/.codex) from this machine — it isn't inside " +
          "the container. Mount it when you run the container (`-v ~/.codex:/root/.codex`) and " +
          "then import, or import on the host."
      );
      return;
    }
    if (!isLocal()) {
      vscode.window.showInformationMessage(
        "Codex import must run on the machine that hosts Hermes Router. This extension is connected to a remote URL."
      );
      return;
    }
    runHrTerminal(["auth", "import-codex"]);
  });

  // ── Polling ──────────────────────────────────────────────────────────────────
  let timer: NodeJS.Timeout | undefined;
  const startTimer = () => {
    if (timer) clearInterval(timer);
    const secs = vscode.workspace.getConfiguration("hermesRouter").get<number>("refreshSeconds", 10);
    timer = setInterval(refresh, Math.max(3, secs) * 1000);
  };
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("hermesRouter")) {
        startTimer();
        void refresh();
      }
    }),
    { dispose: () => timer && clearInterval(timer) }
  );

  startTimer();
  void refresh();
}

export function deactivate() {}

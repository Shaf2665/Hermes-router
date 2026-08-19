---
title: "VS Code Extension"
description: "Use Hermes Router's provider pool in VS Code and Copilot Chat, with health monitoring, dashboard access, and tool-capable model routing."
head:
  - tag: title
    content: Hermes Router VS Code Extension for Copilot
---

The **hermes-router** VS Code extension turns your editor into a control panel for the router
*and* lets you use the router's configured provider pool as a model inside Copilot Chat. It
shows health in-editor and opens the full browser dashboard for configuration.

[![Visual Studio Marketplace](https://img.shields.io/visual-studio-marketplace/v/MohammedShafiq.hermes-router?label=VS%20Marketplace&logo=visualstudiocode)](https://marketplace.visualstudio.com/items?itemName=MohammedShafiq.hermes-router)

It does three things:

1. **Monitor** — a status-bar badge and a live in-editor panel showing configured provider health.
2. **Manage the basics** — restart, run the doctor, update, all from the palette. Everything else
   (API keys, provider models, add-ons, key rotation mode) is configured in the **web
   dashboard** (`/dashboard`), which the extension opens for you in one click — there's one place
   to configure the router, not two.
3. **Use as a model** — pick **hermes-router** in Copilot Chat and your prompts route through
   the configured pool (text *and* tool calling, so it can work in **agent mode** when a
   suitable model is available).

---

## Install

**Easiest:** open the **Extensions** view in VS Code (`Ctrl/Cmd+Shift+X`), search
**hermes-router**, and click **Install**.

Or from a terminal:

```bash
code --install-extension MohammedShafiq.hermes-router
```

Or install a `.vsix` by hand: download it from the
[GitHub releases](https://github.com/Shaf2665/Hermes-router/releases), then in VS Code run
**Extensions → ⋯ → Install from VSIX…**.

> **What you also need:** a hermes-router that's actually running — either locally
> (`hr setup`) or remotely (e.g. a [Hugging Face Space](/deployment/#path-4--hugging-face-space-host-it-online)).
> The extension is a *front-end*; it talks to a router, it isn't the router itself.

---

## First-time setup

After installing, tell the extension where your router is and how to authenticate. Open
**Settings** (`Ctrl/Cmd+,`), search "hermes-router", and set:

| Setting | Default | What to put |
|---|---|---|
| `hermesRouter.baseUrl` | `http://localhost:8319` | Your router's URL. For a remote router, use its full URL (e.g. your Space `https://you-hermes.hf.space`). |
| `hermesRouter.apiKey` | *(empty)* | Any value from your router's `PROXY_API_KEYS`. Fresh routers generate one in `.env` and log it once. Used to read status and to chat. |
| `hermesRouter.hrPath` | `hr` | Path to the `hr` CLI, for the local *manage* actions. Usually leave as-is. |
| `hermesRouter.dockerContainer` | *(empty)* | Set to your container's name to manage a router running in **Docker** (see [below](#using-the-router-in-docker)). |
| `hermesRouter.refreshSeconds` | `10` | How often the dashboard and status bar refresh. |

For a default local router, leave `baseUrl` unchanged but set `apiKey` to the generated
`PROXY_API_KEYS` value before status or chat can authenticate.

---

## Monitoring

### Status bar

A small badge sits in the bottom status bar:

- `✓ hermes-router 11/12` — the router is up and 11 of 12 providers are available.
- It turns into a **warning** if the router is unreachable or down.

Click it to open the dashboard.

### Dashboard

Click the **hermes-router** icon in the activity bar (left edge) to open a live in-editor panel.
It shows, for every provider:

- **Up/down** status and its health rating
- **Latency** and the **model** it's currently using
- Any **key cooldowns** (a key that's resting after a rate-limit)

…plus the overall **cache hit-rate** and the active **key-rotation mode**. It refreshes on its
own every few seconds (the `refreshSeconds` setting), or hit the refresh button in its title bar.

This panel is deliberately compact — an at-a-glance health check, not a config screen. Click
**⬈ Web dashboard** (in the panel or the palette) for the full picture: the live request log,
per-key usage, and every configuration action. See [Monitoring → Web dashboard](/monitoring/#web-dashboard).

---

## Managing the router

Open the Command Palette (`Ctrl/Cmd+Shift+P`) and type "hermes-router" to see every action (the
in-editor panel has buttons for Refresh and Restart; the rest are palette-only):

| Command | What it does |
|---|---|
| **Open Dashboard** | Show the live provider table (in-editor panel) |
| **Open Web Dashboard (browser)** | Open the router's full browser dashboard at `/dashboard` — health, request log, per-key usage, and every config action |
| **Restart Router** | Apply config changes using the local CLI, Docker, or the remote restart endpoint |
| **Run Doctor (diagnose)** | Diagnose install/health problems (`hr doctor`) |
| **Update to Latest** | Upgrade the router (`hr update`) |
| **Import Codex (ChatGPT) Login** | Bring in a ChatGPT-subscription login (`hr auth import-codex`) — the one config action that stays here, since it reads a local OAuth login file the web dashboard can't reach |

> **Adding keys, setting models, toggling add-ons, and changing key rotation mode** all live in
> the **web dashboard** now (open it with the button above) — see
> [Monitoring → Web dashboard](/monitoring/#web-dashboard). This keeps configuration in one place
> instead of split between the extension and the browser.

> **Remote routers:** monitoring and the web dashboard work over HTTP. **Restart** uses the
> authenticated HTTP restart endpoint; it succeeds only when the remote process has a supervisor
> or container restart policy. Doctor, Update, and Import Codex must run on the host and show a
> notice instead.

---

## Using the router in Docker

**Configuring** the router (add keys, set models, toggle add-ons) is done entirely through the
**web dashboard** now, and that works identically whether the router runs bare-metal or in
Docker — it's just HTTP to the router itself, no `hr` CLI needed inside the container. Point your
browser (or the extension's **Open Web Dashboard** button) at the container's mapped port, e.g.
`http://localhost:8319/`.

**Restarting** to apply a change is where Docker needs a little care, since it's an OS-level
operation on the container, not just an HTTP call:

- **Preferred: set `hermesRouter.dockerContainer`** to your container's name in the extension's
  settings. Its own **Restart Router** command then correctly runs `docker restart <container>`
  instead of `hr restart` (which would just kill the container's main process). This also makes
  **Doctor** and **Update** work via `docker exec` — see the `:cli` image setup below.
- **Or use the web dashboard's own "Restart Now" button.** This restarts the router process
  in-place. Inside a container where `router.py` is the main process, that process exiting stops
  the *container* — it only comes back on its own if the container has a restart policy. The
  provided `docker-compose.yml` already sets `restart: unless-stopped`, so Compose users are
  covered automatically. A bare `docker run` with no restart flag would need a manual
  `docker start <container>` afterward — add `--restart unless-stopped` to avoid that.

**1. Run the `:cli` image with a volume** — the standard image is just the router; the **`:cli`**
variant also bundles the `hr` CLI (for the extension's Restart/Doctor/Update/Import-Codex via
`docker exec`), and the volume keeps your keys/settings across restarts:

```bash
docker run -d --name hermes-router -p 8319:8319 --restart unless-stopped \
  -v hermes-data:/app/data -e PROXY_API_KEYS=replace-with-a-long-random-secret \
  shafiq735/hermes-router:cli
```

(On Windows PowerShell, put that on one line — see [Deployment](/deployment/#path-1--docker-easiest-any-os).)

**2. Set `hermesRouter.dockerContainer`** to `hermes-router` in the extension's settings (keep
`baseUrl` = `http://localhost:8319`, `apiKey` = your `PROXY_API_KEYS`).

Codex import (the one config action that stays in the extension, since it reads a local `~/.codex`
OAuth login the web dashboard can't reach) needs `-v ~/.codex:/root/.codex` mounted too — see
[Providers](/providers/#codex-chatgpt-subscription).

---

## Use hermes-router as an AI model (Copilot Chat)

This is the headline feature. The extension registers **hermes-router** as a language model in
VS Code, so you can chat through your configured provider pool right inside Copilot.

1. Make sure the **GitHub Copilot Chat** extension is installed and you're on **VS Code ≥ 1.104**.
2. Open the Chat view, click the **model picker** (the model name near the input box).
3. Choose **hermes-router**.

Prompts are sent through whichever configured provider/model the router picks. Provider pricing
and account limits still apply.
Replies **stream** in just like any built-in model.

### Agent mode (tool calling)

hermes-router supports **tool-capable inference**, so it can be selected in Copilot **agent
mode**. Copilot Agent Mode owns terminal commands, file edits, and MCP tools; Hermes routes the
model requests and tool calls to a confirmed tool-capable provider/model. For reliable agent use,
configure at least one model known to support function calling.

```text
VS Code / Copilot Agent Mode
        ↓ owns tools, terminal, file edits, MCP
Hermes Router
        ↓ routes inference and tool calls
capable provider/model
```

Hermes does not execute tools itself.

> **It's also available to other extensions.** Anything that uses the VS Code `vscode.lm` API
> can select the hermes-router model too — not just Copilot Chat.

---

## Troubleshooting

**Status bar says the router is down / "unreachable"**
: The extension can't reach `baseUrl`. Confirm the router is running (`curl <baseUrl>/health`)
  and that `hermesRouter.baseUrl` points at it. For a remote router, include the full
  `https://…` URL.

**`401` / dashboard is empty**
: `hermesRouter.apiKey` must be one of the router's `PROXY_API_KEYS`. Make them match.

**hermes-router doesn't appear in the Copilot model picker**
: You need **VS Code ≥ 1.104** *and* the **GitHub Copilot Chat** extension. Reload the window
  after installing both.

**"Restart / Doctor / Update" says `hr` isn't on your PATH (or you saw `spawn hr ENOENT`)**
: Those commands use the `hr` CLI, which is the **Linux/macOS/WSL** helper — it doesn't exist on
  a plain **Windows** host, or inside a container that doesn't bundle it:
  - **Docker:** set `hermesRouter.dockerContainer` to your container's name so these commands run
    via `docker exec`/`docker restart` instead (see
    [Using the router in Docker](#using-the-router-in-docker)) — or just use the web dashboard's
    own Restart button, which needs no `hr` at all (works as long as your container has a restart
    policy, e.g. `--restart unless-stopped`).
  - **Windows without Docker:** run the router under **WSL2**, where `hr` works — see
    [Deployment → Windows](/deployment/#path-3--windows).
  - Monitoring, the web dashboard, and "use as a model" all work regardless — only these three
    commands need `hr` (or `docker`) reachable.

**Host-only commands on a remote router**
: Doctor, Update, and Import Codex show a notice because they must run on the router host.
  Restart uses the authenticated HTTP endpoint and requires the remote service/container to
  come back after the process exits.

---

See also: [Deployment](/deployment/) (run the router locally, in Docker, or on a Space) and
[Monitoring](/monitoring/) (the `hr status` / `/v1/status` / Prometheus equivalents).

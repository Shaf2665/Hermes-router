# hermes-router — VS Code extension

A control panel for [hermes-router](https://github.com/Shaf2665/Hermes-router) inside VS Code:
**monitor** your provider pool and **manage** the router without leaving the editor.

## Features

- **Status bar** — at-a-glance health: `✓ hermes-router N/total` providers available (turns
  red/warns when the router is down or unreachable). Click to open the dashboard.
- **Dashboard** (activity-bar sidebar) — live table of every provider: up/down, rating,
  latency, model(s), and key cooldowns, plus cache hit-rate and the active key-rotation mode.
  Auto-refreshes.
- **Manage** (command palette or dashboard buttons):
  - **Restart**, **Run Doctor**, **Update**
  - **Add Provider Key** — opens a terminal running `hr auth add <provider>` (your key is typed
    hidden; the extension never handles it)
  - **Import Codex (ChatGPT) Login** — `hr auth import-codex`
  - **Set Provider Model(s)** — comma-separate for multi-model rate-limit failover
  - **Set Key Rotation Mode** — `round-robin` / `sequential`

## Use it as an AI model (Copilot Chat)

The extension registers **hermes-router** as a language model in VS Code. Open **Copilot Chat**,
click the **model picker**, and choose **hermes-router** — your prompts now route through the
router's configured pool. Provider pricing and limits still apply. It's also available to
any extension via the `vscode.lm` API.

> Requires **VS Code ≥ 1.104** and the **GitHub Copilot Chat** extension (for the chat UI +
> model picker). Supports streamed text **and tool calling**, so it can work in Copilot
> **agent mode** (run commands, edit files, call MCP tools). Configure at least one model known
> to support tools; the router prefers confirmed tool-capable models.

## Requirements

A running hermes-router (locally, or remote e.g. a Hugging Face Space) and — for the *manage*
commands — the `hr` CLI on your PATH. See the
[hermes-router docs](https://hermes-router.vercel.app).

## Settings

| Setting | Default | Description |
|---|---|---|
| `hermesRouter.baseUrl` | `http://localhost:8319` | Router URL. Use your Space URL for a remote router. |
| `hermesRouter.apiKey` | *(empty)* | A value from `PROXY_API_KEYS`. Fresh routers generate one in `.env` and log it once. |
| `hermesRouter.hrPath` | `hr` | Path to the `hr` CLI (for local control actions). |
| `hermesRouter.dockerContainer` | `` | Container name to manage the router via Docker (see below). |
| `hermesRouter.refreshSeconds` | `10` | Dashboard / status-bar refresh interval. |

> **Remote routers:** Monitoring and Restart work over authenticated HTTP against any
> `baseUrl`; Restart requires the remote supervisor/container to bring the process back.
> Doctor, Update, and Import Codex must run on the router host and show a notice remotely.

## Managing a router running in Docker

On Windows (or anywhere the router runs in a container) there's no `hr` on your host. Set
**`hermesRouter.dockerContainer`** to your container's name and the control buttons run against
the container instead:

- **Add Key / Import Codex** → open a terminal running `docker exec -it <container> hr …` (you
  type the key into the container, the extension never sees it), then `docker restart` to apply.
- **Set Model / Rotation** → `docker exec <container> hr …` then `docker restart`.
- **Restart** → `docker restart <container>` (not `hr restart`, which would stop the container).

This needs the **`:cli`** image variant run with a volume so changes persist:

```bash
docker run -d --name hermes-router -p 8319:8319 \
  -v hermes-data:/app/data -e PROXY_API_KEYS=replace-with-a-long-random-secret \
  shafiq735/hermes-router:cli
```

Then set `hermesRouter.dockerContainer` to `hermes-router`. Requires the `docker` CLI on your
PATH (Docker Desktop provides it).

## Install

[![Visual Studio Marketplace](https://img.shields.io/visual-studio-marketplace/v/MohammedShafiq.hermes-router?label=VS%20Marketplace)](https://marketplace.visualstudio.com/items?itemName=MohammedShafiq.hermes-router)

**One-click:** search **hermes-router** in the VS Code Extensions view and click Install, or:

```bash
code --install-extension MohammedShafiq.hermes-router
```

Or grab a `.vsix` from the [releases](https://github.com/Shaf2665/Hermes-router/releases) and
run **Extensions → … → Install from VSIX**.

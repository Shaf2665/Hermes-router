---
title: "VS Code Extension"
description: "Use Hermes Router's provider pool in VS Code and Copilot Chat, with health monitoring, dashboard access, and tool-capable model routing."
head:
  - tag: title
    content: Hermes Router VS Code Extension for Copilot
---

Package update (2026-11-03): [download the VS Code 0.9.6 VSIX](https://github.com/Shaf2665/Hermes-router/releases/download/vscode-v0.9.6/hermes-router-0.9.6.vsix). Use extension version 0.9.6 or newer for administrator controls. See [Setup](/setup/) for updated Docker images and migration.


The Hermes Router extension connects Copilot Chat to your provider pool and offers optional owner controls: provider status, remote restart and the browser dashboard. Hermes routes model requests and tool calls; Copilot owns terminal commands, file edits and MCP tools.

These instructions cover the **foundation source preview**. Previously published Marketplace/VSIX builds may predate the administrator credential command. Use the extension built from the same preview checkout when connecting to the upgraded router; installing an older published extension does not upgrade its authentication behavior.

## Install the preview

You need VS Code 1.104 or later and the GitHub Copilot Chat extension for the model picker. Start the router first using [Setup](/setup/).

From this checkout:

```bash
cd vscode-extension
npm ci
npm run compile
npm run package
```

In VS Code, run **Extensions: Install from VSIX…** and choose the generated file. For a published release, use the [Marketplace listing](https://marketplace.visualstudio.com/items?itemName=MohammedShafiq.hermes-router) or [GitHub releases](https://github.com/Shaf2665/Hermes-router/releases); check that release's migration notes before mixing versions.

## Connect chat with an application key

Open the router's browser dashboard, sign in using `ADMIN_API_KEY` from your local configuration, and open **Connect**. Create an application key, copy it, and optionally send the explicit small test request. An upstream charge may apply. Existing `PROXY_API_KEYS` remain valid for inference.

In VS Code Settings, search for `hermesRouter`:

| Setting | Value |
|---|---|
| `hermesRouter.baseUrl` | Router origin, such as `http://localhost:8319`. A trailing `/v1` is accepted. Use HTTPS or a local SSH tunnel for a remote router. |
| `hermesRouter.apiKey` | The application key, for chat/inference only. Keep your existing setting when upgrading. |
| `hermesRouter.hrPath` | `hr`, or the path to the Bash CLI for Linux/macOS/WSL host controls. |
| `hermesRouter.dockerContainer` | Optional container name for controls through the host's Docker CLI. |
| `hermesRouter.refreshSeconds` | Status refresh interval; default 10 seconds. |

Open Copilot Chat, open its model picker, and select **hermes-router**. Text streams through your configured providers. Tool calling in agent mode requires a configured model with confirmed tool support; provider prices and limits still apply.

## Optional administrator controls

Application keys no longer authorize `/v1/status`, global activity, configuration or remote restart. A chat connection can work while the owner panel asks for an administrator credential.

Run **Hermes Router: Set Administrator Credential** from the Command Palette. Enter the router's `ADMIN_API_KEY`. The extension stores it in **VS Code SecretStorage**, scoped to the router URL, and uses it for owner status and remote restart. It does not write it to settings or replace `hermesRouter.apiKey`. Run the command with an empty value to remove it. Configure the credential again after changing router URLs.

The browser dashboard signs in separately using an expiring HttpOnly session. Restarting the router signs browser sessions out. Never paste the administrator credential into an application-key setting, SDK snippet or shared workspace configuration.

| Command | Behavior |
|---|---|
| **Open Dashboard** | Owner provider status in the editor. Requires the SecretStorage administrator credential. |
| **Open Web Dashboard (browser)** | Opens `/dashboard`; Providers, Connect, Activity and Settings remain there. |
| **Refresh Status** | Refreshes the owner panel; a public health check helps distinguish an unreachable router from missing owner access. |
| **Set Administrator Credential** | Stores or removes the optional router-scoped owner credential. |
| **Restart Router** | Uses local `hr restart`, host `docker restart`, or the authenticated remote restart API, depending on settings. |
| **Run Doctor (diagnose)** | Runs `hr doctor` on a supported local/Docker host. Remote routers show host instructions. |
| **Update to Latest** | Runs the local update helper. Docker users pull/rebuild and recreate their container; remote routers require host access. |
| **Import Codex (ChatGPT) Login** | Runs the local/Docker import helper. Remote routers require host access. |

Provider credentials, model overrides, key rotation, optional features, application access and instances are managed in the browser dashboard. See [Monitoring](/monitoring/).

## Docker and Windows controls

With a router in Docker, set `hermesRouter.dockerContainer` to its container name. **Restart Router** then runs `docker restart <container>` on the host. Doctor and Codex import require the `:cli` image and the host Docker CLI. Updating an image requires pulling/rebuilding and recreating the container; the extension reports this instead of running an in-container update.

The browser/remote restart endpoint only reports success on an installation with a supported restart helper. Native Windows and a minimal Docker image can return **409** with a remedy; a container restart policy does not make a missing helper supported. Use `docker restart <container>` on the host, or stop the native Windows router and rerun `start.ps1` in PowerShell. See [Setup](/setup/) for the preview's persistent Compose configuration and native Windows installation.

Native Windows supports browser configuration and chat. The extension's local `hr` controls still require Linux/macOS/WSL, or Docker controls through the host Docker CLI. A remote owner credential does not grant SSH or host command access.

## Troubleshooting

- **Chat returns 401:** check `hermesRouter.apiKey` is a live application key for this router. A revoked key fails new requests immediately.
- **Owner panel returns 401/403 or asks for a credential:** run **Set Administrator Credential** and use `ADMIN_API_KEY`. Changing the application key will not grant owner access.
- **Router unreachable:** check `baseUrl`, the running process and `/health`. A live process can still have no usable provider; verify a real request from Connect.
- **Model missing from Copilot's picker:** check VS Code/Copilot Chat requirements and reload the editor after installing the preview VSIX.
- **`hr` not found:** use the documented native Windows start/stop commands, WSL, or configure Docker controls. Browser configuration and inference do not require `hr` on the editor's host.
- **Restart rejected:** follow the returned remedy. Success is only reported after a successful API response; a network failure is not treated as a completed restart.

See [Setup](/setup/), [Providers](/providers/) and [Monitoring](/monitoring/) for router configuration.

# Hermes Router for VS Code

Use your own AI providers in **Copilot Chat** through one Hermes Router connection.
Choose **hermes-router (provider pool)** in the model picker; Hermes selects a
model from your configured providers and tries another eligible model if it fails.

The extension also shows router status and opens your browser dashboard. You can
connect to Hermes on your computer, in Docker, or on a server.

**The extension connects to a router; it does not install or host one.** Hermes
handles AI requests. Copilot handles agent actions such as file edits, terminal
commands and tools. Provider charges and quotas still apply.

## Before you start

You need:

- **VS Code 1.104 or newer**, with Copilot Chat available and enabled for your account.
- A running Hermes Router with at least one working provider/model.
- An **application key** created in that router's **Connect** page.

New to Hermes? Follow [Local setup](setup.md) or [Server setup](deployment.md) first. Send a test
message in the browser dashboard before connecting the extension.

## 1. Install the extension

In VS Code, open **Extensions** (`Ctrl+Shift+X` on Windows/Linux, `Cmd+Shift+X` on Mac),
search for **hermes-router** by **MohammedShafiq**, and select **Install**.
The [Marketplace listing](https://marketplace.visualstudio.com/items?itemName=MohammedShafiq.hermes-router)
also has an Install button.

For a downloaded package, open the Command Palette (`Ctrl+Shift+P`, or `Cmd+Shift+P`
on Mac), run **Extensions: Install from VSIX…**, and select the `.vsix` file.
[GitHub releases](https://github.com/Shaf2665/Hermes-router/releases) list published
packages. Version **0.9.7** fixes the dashboard getting stuck on “Loading…”.

**Context sync needs extension 0.9.10**, which is built from source (see
[Build from source](#build-from-source-developers)). Marketplace 0.9.9 uses an older
sync path that can show a 40K fallback; do not downgrade to 0.9.8.

If prompted, reload VS Code. You do not need Node.js or to build source to install a VSIX.

## 2. Enter your router address and application key

Open **Settings** (`Ctrl+,` or `Cmd+,`) and search for **hermesRouter**.
Use your **User** settings so a key is not saved in a shared project's settings file.

| Setting | What to enter |
| --- | --- |
| **Hermes Router: Base Url** (`hermesRouter.baseUrl`) | `http://localhost:8319` for a router on this computer; your tunnel or HTTPS address for a server |
| **Hermes Router: Api Key** (`hermesRouter.apiKey`) | The application key from **Connect** |

Use the **application key**, not your provider key or administrator key. A trailing
`/v1` in Base Url is accepted, but you can leave it off.

For a server reached through SSH, keep the tunnel open. If VS Code runs an extension
inside WSL, a container or Remote SSH, `localhost` means that extension's machine.
Use an address reachable from where the extension runs.

## 3. Choose Hermes in Copilot Chat

1. Open **Chat** in VS Code.
2. Open the model picker beside the chat input.
3. Select **hermes-router (provider pool)**.
4. Send a short message, such as “Reply with hello.”

If **hermes-router** is missing from the picker, confirm your models' context and output
limits in the dashboard first: 0.9.10 hides models with unknown capacity. See
[Context synchronization](#context-synchronization-0910).

If it replies, your connection works. You can then use Agent mode when your chosen
providers support tool calling. Adding providers is optional; one working provider
is enough to start.

## 4. Enable the status panel (optional)

Chat can work even if the Hermes status panel asks for an administrator credential.
The panel needs owner access to display all providers and usage.

1. Open the Command Palette.
2. Run **Hermes Router: Set Administrator Credential**.
3. Enter the same **administrator key** you use for the browser dashboard.
4. Run **hermes-router: Refresh Status** or open the Hermes sidebar.

The extension keeps this credential in **VS Code SecretStorage**, separately for
each router address. It does not replace the application key. Run the same command
with an empty value to remove it; set it again if you change router addresses.

Use **hermes-router: Open Web Dashboard (browser)** to add providers, create keys,
change settings or inspect activity. The browser has its own sign-in session.

## All extension settings

In VS Code Settings, search for `hermesRouter`:

| Setting | Value |
|---|---|
| `hermesRouter.baseUrl` | Router origin, such as `http://localhost:8319`. A trailing `/v1` is accepted. Use HTTPS or a local SSH tunnel for a remote router. |
| `hermesRouter.apiKey` | The application key, for chat/inference only. Keep your existing setting when upgrading. |
| `hermesRouter.hrPath` | `hr`, or the path to the Bash CLI for Linux/macOS/WSL host controls. |
| `hermesRouter.dockerContainer` | Optional container name for controls through the host's Docker CLI. |
| `hermesRouter.refreshSeconds` | Context limits and status refresh interval; default 10 seconds, minimum 3. Each new chat request also checks context settings. |

## Context synchronization (0.9.10+)

Think of the dashboard as the control knob and the extension as its display. In the browser dashboard, open **Settings → Context length** and choose **Minimum model context**. The extension reads the effective input/output limits using its application key, refreshes them every 10 seconds by default, and checks again before each chat request. No administrator credential is needed for that read.

Open **Confirm model capacities** if discovery has not supplied both limits. Select the model, enter its provider-documented **Total context tokens** and **Maximum output tokens**, then click **Save confirmed limits**. Use the limits for your actual provider/account; selecting 256K cannot enlarge a smaller model. Unknown capacities are excluded from extension discovery, even in Auto mode.

The selected minimum filters eligible models and sets the extension's target budget. **Auto** uses confirmed model capacities. Hermes reserves room for output and uses the lowest safe input/output budgets across the caller's eligible models. Provider ceilings can therefore make the displayed total smaller than the selected minimum.

For one model with a confirmed 512,000-token context and at least 8,192 output tokens, choosing **256K** advertises **247,808 input + 8,192 output = 256,000 total**. With **Auto**, that model advertises **503,808 input + 8,192 output**.

Saves apply to new requests without restarting Hermes. A conversation that still fits a reduced limit can continue; an oversized conversation must be shortened. A change between the final check and sending returns a retry error. Existing admitted responses keep their settings. Token counting uses a conservative UTF-8 byte estimate, including tools, so usable text may be smaller than a provider-specific tokenizer would allow.

See [Context synchronization](context-sync.md) for API details, a compatibility table, and a step-by-step verification.

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

Provider credentials, model overrides, key rotation, optional features, application access and instances are managed in the browser dashboard. See [Monitoring](monitoring.md).

## Docker and Windows controls

With a router in Docker, set `hermesRouter.dockerContainer` to its container name. **Restart Router** then runs `docker restart <container>` on the host. Doctor and Codex import require the `:cli` image and the host Docker CLI. Updating an image requires pulling/rebuilding and recreating the container; the extension reports this instead of running an in-container update.

The browser/remote restart endpoint only reports success where Hermes can restart itself: a CLI-managed process, or a Docker container started with `HR_CONTAINER_RESTART=1` and a restart policy (the bundled `docker-compose.yml` sets both). Native Windows and other containers return **409** with a remedy; use `docker restart <container>` on the host, or stop the native Windows router and rerun `start.ps1` in PowerShell. See [Setup](setup.md) for the persistent Compose configuration and native Windows installation.

Native Windows supports browser configuration and chat. The extension's local `hr` controls still require Linux/macOS/WSL, or Docker controls through the host Docker CLI. A remote owner credential does not grant SSH or host command access.

## Troubleshooting

- **Chat returns 401:** check `hermesRouter.apiKey` is a live application key for this router. A revoked key fails new requests immediately.
- **Owner panel returns 401/403 or asks for a credential:** run **Set Administrator Credential** and use `ADMIN_API_KEY`. Changing the application key will not grant owner access.
- **Router unreachable:** check `baseUrl`, the running process and `/health`. A live process can still have no usable provider; verify a real request from Connect.
- **Still shows about 40K in Marketplace 0.9.9:** check `hermesRouter.apiKey` is an active application key from dashboard **Connect**, not the dashboard administrator key. HTTP 401 on `/v1/models` triggers its silent fallback. Keep `hermesRouter.contextWindow` at 0. Source 0.9.10 reports sync failures instead of inventing a budget; run **Hermes Router: Check Context Sync** to see the connected URL, actual limits, or error.
- **Router update required / client-config returns 404:** update the router as well as the extension; a container restart alone does not replace its image.
- **Model missing from Copilot's picker:** check VS Code/Copilot Chat requirements, the application key's scope, and confirmed context/output limits in the browser dashboard. At least one eligible model must fit the minimum; lower the minimum or add a compatible model if necessary.
- **Dashboard stuck on “Loading…”:** reload after installing the current VSIX, which retains the published 0.9.7 dashboard fix. Owner status still requires **Set Administrator Credential**.
- **`hr` not found:** use the documented native Windows start/stop commands, WSL, or configure Docker controls. Browser configuration and inference do not require `hr` on the editor's host.
- **Chat prints `<invoke ...>` or stops after a file-reading instruction:** update the router. Older Anthropic adapters dropped tool definitions, native `tool_use` responses, and tool results. The fix preserves them for streamed and ordinary requests, so Copilot can run its selected tools and continue. Start a new chat after updating to avoid reusing the broken conversation. XML-looking text is never executed as a tool call. Ask mode supports read-only codebase research; choose Agent when you want edits or commands. See [VS Code agent harnesses](https://code.visualstudio.com/docs/agents/run/agent-harnesses) and [Anthropic tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools).
- **Three ready providers, but only one receives requests:** context size, tool support, access scopes and health determine eligibility. At a 1M minimum, a 131K model is excluded even for a short prompt. Unknown capacities are also excluded. Save documented capacities in Settings, or choose a smaller minimum if you want smaller models in the pool. See [verified model limits](context-sync.md#verified-capacities-for-the-configured-models).
- **Forced tool use rejected by Claude:** Hermes preserves `required`/named tool choices; model support varies. Sonnet 5.5 rejects forced tool use, so use `auto` for that model. Hermes does not silently weaken a required tool call to an optional one. See [Anthropic tool choice restrictions](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools#forcing-tool-use).
- **Restart rejected:** follow the returned remedy. Success is only reported after a successful API response; a network failure is not treated as a completed restart.

See [Setup](setup.md), [Providers](providers.md) and [Monitoring](monitoring.md) for router configuration.

## Build from source (developers)

You need VS Code 1.104 or later and the GitHub Copilot Chat extension for the model picker. Start the router first using [Setup](setup.md).

From this checkout:

```bash
cd vscode-extension
npm ci
npm run check
npm test
npm run compile
npm run package
```

In VS Code, run **Extensions: Install from VSIX…**, choose `hermes-router-0.9.10.vsix`, then run **Developer: Reload Window**. For a published release, use the [Marketplace listing](https://marketplace.visualstudio.com/items?itemName=MohammedShafiq.hermes-router) or [GitHub releases](https://github.com/Shaf2665/Hermes-router/releases); check that release's migration notes before mixing versions.

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

New to Hermes? Follow [Local setup](https://hermes-router.vercel.app/setup/) or
[Server setup](https://hermes-router.vercel.app/deployment/) first. Send a test
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

## Choose your context size (0.9.9+)

1. Open the router's browser dashboard.
2. Go to **Settings → Context length**.
3. Choose **128K** to allow only models with at least 128,000 tokens of context,
   or **16K** to allow models with at least 16,000. The change saves immediately.

Extension **0.9.9+** follows this setting by default and updates the model picker
on its next refresh (normally within 10 seconds). If you previously set
`hermesRouter.contextWindow`, reset it to **0** to follow the dashboard.
Use **hermes-router: Refresh Status** to refresh now; start a new chat if an
existing chat keeps its old budget.

Tokens measure text size. Context includes conversation history, tools and room
for the reply. With 128K selected and the default `hermesRouter.maxOutputTokens`
of 8,192, the extension allows 119,808 input tokens. The reply allowance must be
smaller than the total context window.

**Auto** on the dashboard uses request size and any client requirement. In that
mode, the extension falls back to **Max 40K** (32,000 input + 8,192 output).
You can set a positive `hermesRouter.contextWindow` to request more context for
this client. The dashboard minimum always applies, even if a client requests less.

Hermes excludes models with unknown or insufficient capacity, including during
failover. Larger requests can require a larger model. If no model fits, you get an
error. Changing the setting does not increase a provider's actual capacity.

Upgrade the router as well as the extension: use Docker image `context-20261104.1`
or newer. The older `custom-20261104` image predates these controls. Extension 0.9.8 supports manual context settings but does not follow
the dashboard; earlier extensions have a fixed 40K budget. See
[context configuration](https://hermes-router.vercel.app/configuration/#context-windows)
for capacity overrides and token-estimation limits.

## Useful commands and settings

Open the Command Palette and search for `hermes-router`:

| Command | Purpose |
| --- | --- |
| **Open Dashboard** | Open the provider/status panel; administrator credential required |
| **Open Web Dashboard (browser)** | Open the browser dashboard for setup and management |
| **Refresh Status** | Reload panel and status-bar information |
| **Set Administrator Credential** | Set or remove optional owner access |
| **Restart Router** | Restart through supported local, Docker or server controls |
| **Run Doctor (diagnose)** | Run the `hr` diagnostic command on supported local/Docker installs |
| **Update to Latest** | Use the local update helper, or show Docker/remote update guidance |
| **Import Codex (ChatGPT) Login** | Run the import helper on a supported local/Docker installation |

Most users need only `baseUrl` and `apiKey`. Optional settings:

- `hermesRouter.refreshSeconds`: how often the status panel refreshes; default 10.
- `hermesRouter.hrPath`: path to the Bash `hr` helper on Linux/macOS/WSL.
- `hermesRouter.dockerContainer`: container name for local Docker controls.

Native Windows supports chat and browser configuration without Bash. Its `hr`
commands need WSL or the Docker CLI image. For Docker controls, the computer running
the extension needs access to Docker; use the `context-20261104.1-cli` image for commands
that call `hr` inside the container. A remote URL does not grant SSH or host access.

Some hosts cannot restart from the dashboard. Use `docker restart hermes-router`,
the server's service manager, or stop and rerun `start.ps1` as appropriate.

## Troubleshooting

| Problem | Try this |
| --- | --- |
| Model missing from the picker | Check VS Code/Copilot Chat requirements, enable the extension, then run **Developer: Reload Window**. |
| Chat returns 401 | Replace `hermesRouter.apiKey` with a current application key from Connect. |
| Status panel asks for owner access or returns 401/403 | Run **Set Administrator Credential** with the separate administrator key. |
| Panel stays on “Loading…” | Install 0.9.7 or newer, then reload VS Code. |
| Router unreachable | Open its dashboard URL; check the running process, tunnel and Base Url. |
| All providers exhausted | Send a test in Connect; check provider keys, selected models, quotas and model eligibility. |
| No model fits the context | Use a smaller window or configure a model with sufficient known capacity. |
| `hr` not found or restart unsupported | Use the terminal/Docker/service manager on the machine hosting Hermes. |

## Build the source preview (developers)

Only use this when you need unpublished changes. From a matching updated source
checkout, with Node.js/npm installed:

```bash
cd vscode-extension
npm ci
npm run compile
npm run package
```

Install the generated VSIX as described above. For context routing, update the
router from the same checkout too. No credentials belong in the package.

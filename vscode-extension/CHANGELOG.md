# Changelog

## 0.9.4

- **Match generated router keys.** The extension no longer assumes the retired
  `sk-router-1` default; set `hermesRouter.apiKey` to the key generated in `.env`.
- **Keep host-only actions on the host.** Update and Codex import now show a clear notice for
  remote routers instead of accidentally running against the local machine.

## 0.9.3

- **Fix remote control buttons.** Restart now uses the router HTTP restart endpoint for remote
  routers, and Doctor clearly explains when it must be run on the host/container instead of
  appearing broken.
- **Simpler web dashboard.** Setup is now near the top, provider health is card-based, and
  dense tables/logs/cache/key details are collapsed under advanced sections.

## 0.9.2

- **Friendlier dashboard for non-technical users.** The VS Code sidebar now opens with a
  plain health summary, quick actions, key/error/token/spend cards, and a provider attention
  list instead of a dense table.
- Keeps advanced configuration in the browser dashboard while making the in-editor panel easier
  to understand at a glance.

## 0.9.0

- **Simplified the extension — all configuration moved to the web dashboard.** The in-editor
  panel is now a compact, at-a-glance health view: provider status, latency, the currently
  active model per provider (not the full model list), and a Refresh/Restart button. Adding
  API keys, setting provider models, toggling add-ons, and changing the key rotation mode are
  now done in the **web dashboard** (`/dashboard`), which now has real forms for all of it — one
  place to configure the router, not two.
- Removed the **Add Provider Key**, **Set Provider Model(s)**, and **Set Key Rotation Mode**
  commands (superseded by the web dashboard). **Import Codex (ChatGPT) Login** stays in the
  extension — it reads a local `~/.codex` OAuth login the web dashboard can't reach.
- The router itself gained new endpoints backing this
  (`POST /v1/config/keys/<provider>`, `POST`/`DELETE /v1/config/model/<provider>`,
  `POST /v1/config/features/<name>`, `POST /v1/config/restart`) — same proxy-key auth as
  everything else, writing to the same `auth.json`/`.env` files `hr` already uses.

## 0.8.1

- **Fix: dashboard panel could get stuck on "Loading…".** The shared refresh now updates the
  webview unconditionally (a `/health` blip no longer blocks it), and the panel requests its
  own data once its script is ready — so a lost initial message can't leave it hanging. A bad
  `hermesRouter.apiKey` now shows a clear error instead of a blank panel.

## 0.8.0

- **Open the full web dashboard in your browser.** New **⬈ Web dashboard** button (and a globe
  icon in the panel header) opens the router's built-in browser dashboard at `/dashboard` —
  a richer live view with the request log, per-key budget usage, cache stats, and provider
  health. The compact in-editor panel stays for at-a-glance monitoring. Command:
  **hermes-router: Open Web Dashboard (browser)**.

## 0.7.0

- **Toggle add-ons from the dashboard.** The dashboard now has an **Add-ons** panel — click a
  feature (semantic cache, persistent cache, fast routing, metrics auth, cost currency) to
  enable/disable it; the extension runs `hr features …` and restarts the router for you.
  Config-driven add-ons (per-key budgets, local model) show their status and how to manage them.
  Works for both local and Docker-managed routers.
- **Tidier layout.** Provider table columns (Rating / Latency / Tokens) are right-aligned with
  tabular numbers, the provider column takes the slack, and multi-model rows read more cleanly.

## 0.6.1

- **Add-ons at a glance.** The dashboard now shows which optional add-ons are enabled (semantic
  cache, persistent cache, fast routing, …) — reads the new `features` block from `/v1/status`.
  Manage them from the terminal with `hr features list|enable|disable`.

## 0.6.0

- **Estimated spend in the dashboard.** When the router has cost awareness configured, the
  dashboard header shows total estimated **spend** alongside tokens served (reads the new
  `cost_usd` field from `/v1/status`). Free providers/subscription plans count as `$0`.

## 0.5.2

- **Per-model capability in the dashboard.** Multi-model providers now show each model with its
  own rating and capability flags (e.g. `gemini-2.5-pro (r1 · tools · reasoning)`), reflecting the
  router's new per-(provider, model) smart routing — so you can see why a non-primary model gets
  picked for harder or tool-using requests. Reads the new `model_caps` field from `/v1/status`.

## 0.5.1

- **Fix duplicate model display.** When a provider has multiple models configured, the dashboard
  no longer shows the primary model twice (once on its own line and again in the full list).
  Only the comma-separated list is shown when multiple models are set.

## 0.5.0

- **Usage in the dashboard.** The dashboard now shows a **Tokens** column per provider, total
  tokens served, semantic-cache hits, and (when configured) per-key rate-limit/budget usage —
  all read from the router's `/v1/status`. Pairs with the router's new local-model provider,
  per-key budgets, semantic caching, and `/v1/usage` endpoint.

## 0.4.0

- **Manage a router running in Docker.** New `hermesRouter.dockerContainer` setting — set it to
  your container's name and the control actions run against the container instead of the host:
  **Add Key / Import Codex** open a terminal running `docker exec -it <container> hr …` (you type
  the key inside the container, then it `docker restart`s to apply); **Set Model / Rotation** run
  `docker exec <container> hr …` then restart; **Restart** runs `docker restart <container>`
  (never `hr restart`, which would kill the container's main process).
- Requires the new **`:cli`** image variant (e.g. `shafiq735/hermes-router:cli`) run with a
  mounted volume (`-v hermes-data:/app/data`) so keys/model/rotation persist across restarts.
- **Update** and **Import Codex** show Docker-specific guidance instead of running (you update a
  container by pulling a new image; the Codex login lives on your machine, not the container).

## 0.3.1

- **Friendlier control errors when `hr` isn't installed.** The Restart / Add Key / Model /
  Rotation commands shell out to the `hr` CLI, which only exists on Linux/macOS/WSL — not on a
  Windows host or when the router runs in Docker. Previously these failed with a cryptic
  `spawn hr ENOENT` / "term 'hr' is not recognized". The extension now detects a missing `hr`
  and shows clear, Docker-aware guidance (set keys via `-e <PROVIDER>_API_KEYS=…`, use
  `docker restart`) with a link to the docs. Monitoring is unaffected and keeps working.

## 0.3.0

- **Agent-mode tool calling.** The hermes-router model now supports tool/function calling, so it
  works in **Copilot agent mode** (run commands, edit files, call MCP tools). Tool definitions and
  results are translated both ways; the router routes tool requests only to tool-capable providers.

## 0.2.0

- **Use hermes-router as an AI model.** Registers a Language Model provider, so hermes-router
  appears in **Copilot Chat's model picker** (and is usable by any `vscode.lm` consumer);
  prompts route through the router's free pool with streamed replies.
- Requires VS Code ≥ 1.104. v1 is text chat; agent-mode tool-calling is planned.

## 0.1.0

Initial release — a control panel for hermes-router.

- Status-bar health indicator (providers available / total, rotation mode).
- Dashboard sidebar: live per-provider health, rating, latency, model(s), key cooldowns,
  cache hit-rate, and rotation mode (auto-refreshing).
- Commands: Restart, Doctor, Update, Add Provider Key, Import Codex login, Set Model(s),
  Set Rotation Mode.
- Works against a local or remote (`baseUrl`) router; control commands require a local `hr`.

# Hermes experience audit and proposed direction

Date: 2026-11-02. Scope: repository inspection only; no application changes, installs, deployments or live provider requests. Audience confirmed by user: individuals and teams, with a simple personal mode.

## What already works as a foundation

- One Flask app serves both inference and a self-contained browser dashboard (`router.py:3740`, `router.py:5499`). Keep the dashboard available without a separate Node service.
- Existing overview includes plain status, endpoint/model information and a setup checklist (`router.py:4064`, `router.py:4086`). Improve these rather than creating a parallel admin app.
- Provider health, keys, models, feature toggles, usage, request logs and external/Docker instances already have APIs and UI (`router.py:4025`). Most new UX can reuse these contracts.
- VS Code registers a language-model provider and has separate local/Docker/remote control paths (`vscode-extension/src/extension.ts:29`, `vscode-extension/src/extension.ts:77`). Do not duplicate all browser administration inside the extension.
- Local models are a supported backend; onboarding must not require a cloud key for everyone.

## Priority findings

| Priority | Evidence | Consequence | Smallest complete improvement |
|---|---|---|---|
| P0 | `_auth_check()` accepts any `PROXY_API_KEYS` entry (`router.py:3694`); configuration, key creation and instance handlers use the same check (`router.py:6346`, `router.py:6443`). | A teammate/app key is currently also an administration credential. Provider scope and budget restrictions on inference do not constitute a safe team boundary. | Introduce a distinct owner/admin credential and server-enforced authorization for config, instances and global monitoring. App keys can invoke inference and read their own usage only. Explicitly migrate existing single-user installs without lockout; do not silently grant new keys admin rights. |
| P0 | Keys are process-loaded and revocation reports `restart_required` (`router.py:6325`, `router.py:6439`); browser confirmation says access ends after restart (`router.py:5402`). | Revoking a leaked/team key does not immediately revoke access. New keys can appear saved while being unusable. | Live, atomic key metadata reload for creation/revocation/scope changes, with tests that active requests and persisted state remain consistent. Pending vs active state must be truthful during migration. |
| P0 | `renderProviderCards()` interpolates `p.model` into both HTML and an attribute without existing `esc`/`attr` helpers (`router.py:4721`). Dashboard credential is retained in localStorage (`router.py:4439`, `router.py:4476`). | Provider/model metadata is not safely rendered at this boundary; a script injection could reach the persisted dashboard key. This is a static finding, not an exercised exploit. | Use text-safe rendering/appropriate escaping everywhere status or configuration enters HTML; add malicious model-name browser regression. Provide explicit sign-out and session-only persistence by default. A later admin-session design should use HttpOnly/SameSite cookies with CSRF protection rather than putting owner credentials in application snippets. |
| P1 | The UI offers eight top-level pages, including two different key pages plus Instances and Add-ons (`router.py:4026`). | First-time personal users face administrative structure before getting one answer. | Four everyday destinations: Home, Connections, Activity, Settings. Add Access for team owners. Move instances, raw model settings and routing internals under Advanced settings. |
| P1 | Every five seconds `refresh()` fetches status, usage, logs, proxy keys and instances, regardless of selected page (`router.py:4443`, `router.py:4484`, `router.py:4495`). Any failed response aborts the entire render (`router.py:4512`). | Hidden panels create work; a slow/offline instance can make the whole dashboard feel broken. | Poll only visible data; prohibit overlapping polls; pause/back off when hidden or disconnected. Render each panel independently with last-success time and stale/error state. Load static config on demand. |
| P1 | Setup checklist only checks key/health/restart (`router.py:4088`). CLI verifies `/health` then prints completion (`scripts/setup.sh:141`, `scripts/setup.sh:160`). | A reachable server is mistaken for a usable inference connection; users still must assemble their client configuration. | Finish onboarding with an explicit, bounded test request and a copyable client configuration. Separate installed, connected, tested and connected-to-client states. Never spend provider quota automatically just because a page opened. |
| P1 | `_trigger_restart()` silently skips missing `scripts/restart.sh` (`router.py:993`) and launches `/usr/bin/env bash` (`router.py:999`); API nevertheless returns “restarting” (`router.py:6267`). Minimal Dockerfile copies only router and requirements; native Windows cannot use that Bash launch. | Saving configuration can trap users behind a restart button that cannot apply it. | Return actual restart capability and failures. Prefer safe live application of supported config. For deployment-level settings show an exact command suited to the detected install and preserve pending changes. Never fake successful restart by observing an already-running `/health`. |
| P1 | Compose requires `.env`, builds the minimal image and defines no persistent volume (`docker-compose.yml:1`). The separate CLI image persists auth/state under `/app/data` (`Dockerfile.cli:42`). | Clean installs have competing paths; dashboard changes in ephemeral containers may disappear on recreation. | One recommended persistent Docker path, with optional advanced source-build path. Document and test config survival across container replacement; generate owner bootstrap secret instead of shipping a usable default. |
| P1 | `get.sh` does not use fail-fast and prints success after unguarded `git pull`/`clone` (`get.sh:13`, `get.sh:39`, `get.sh:44`). Installer venv creation similarly continues on failure (`install.sh:56`). | Installer output may claim success after an important prerequisite step failed. | Explicit failure handling at clone/update/venv/dependency/symlink steps, preserving an existing install on error; exercise scripts with failing stub commands. |
| P2 | Installed dependency check imports flask/waitress/requests, skipping requirement reconciliation and tiktoken (`install.sh:67`). | Re-running installer can leave stale requirements or omit the token estimator while reporting dependencies installed. | Reconcile `requirements.txt` with the environment on install/update and verify the supported set; retain the intentional runtime fallback if tiktoken is unavailable. |
| P2 | Setup only counts keys in auth.json (`scripts/setup.sh:42`), says no key means no responses (`scripts/setup.sh:67`), ignores configured local models/env-based keys, and offers reboot persistence only via systemd (`scripts/setup.sh:127`). | Local-model and non-Linux users get misleading or incomplete guidance. | A shared readiness/config endpoint that recognizes local and cloud sources; explicit per-OS lifecycle instructions and one recommended start path. |
| P2 | Extension reads key from normal settings (`vscode-extension/src/extension.ts:12`, `vscode-extension/package.json:69`). | Users may put credentials in workspace settings; separate admin/inference auth would break the current restart use unless migrated. | A connection command with SecretStorage for inference credential, backward-compatible migration, and an optional distinct admin connection. Keep remote restart unavailable to ordinary inference keys. |

## Concrete proposed user flow

Default personal mode has no team-creation ceremony. Start with the local owner bootstrap credential or authenticated owner session, never an unauthenticated network-wide setup endpoint.

1. **Connect an AI source.** Choose cloud provider, local model, or a custom compatible endpoint. Reveal only fields needed for that source. Explain that provider credentials stay on the router. Recognize existing configuration rather than asking again.
2. **Check the connection.** A named “Send test message” action explains it sends a small request; show success, latency and selected provider/model, or one actionable error. Do not infer success from a health probe alone. Allow skip without claiming completion.
3. **Connect your app.** Choose VS Code/Copilot, an OpenAI-compatible app, Anthropic-compatible app, or SDK. Copy endpoint, model and an inference-only key. Use the URL appropriate to where the app runs; do not assume a remote browser's localhost is the server.
4. **Start using Hermes.** Home answers “Is it ready?”, “What happened recently?” and “What needs attention?”. Show a clear route trace for a selected request: app → chosen model → retry/fallback → response. Do not reveal secret credentials or full prompt content by default.

Returning users see Home, not the wizard. Interrupted setup resumes using actual readiness, not a browser-only “completed” checkbox.

## Navigation and appearance

- **Home:** readiness, next action when blocked, recent activity, simple usage summary, copy connection details.
- **Connections:** connected sources with health and usage; adding a source includes its keys and models in one place. Preserve provider order and known model choices; advanced overrides remain available within the source details.
- **Activity:** requests, failures and provider route detail; usage/spend view can share this destination. Show uptime scope clearly because current counters reset on process restart.
- **Access (team owners only):** app/person keys, names, limits and allowed providers, create/copy once/revoke. The first team step is an explicit “Share access” action, not a tenant-management suite. Member identities/invitations/roles are a separate later feature, not something key labels should pretend to implement.
- **Settings:** routing defaults, persistence, service/update status; Advanced contains instances and detailed knobs.

Recommended visual direction: calm, readable operational UI; cool off-white content canvas, dark navy navigation, teal primary actions, amber warnings and red failures. Use a system sans-serif stack with a restrained scale and tabular numerals for metrics. The distinguishing element is a readable request route trace, not decorative graphs. Keep consistent action labels, visible keyboard focus, persistent field labels, error announcements, reduced motion, mobile navigation and contrast checks. Personal mode hides complexity, not functionality or authorization checks.

## Installation and website

### Platform work

- Docker: recommended persistent Compose/CLI-image recipe; explicit volume and update/rollback commands; no required manual editing before first browser visit. Validate secret persistence, port mapping and restart behavior.
- Windows: either provide a maintained PowerShell installer/launcher or clearly recommend Docker, with native Python as an advanced path. Do not display Bash-only commands as the main Windows action. Choose this in the release plan; an untested wrapper is not native support.
- Linux/macOS: retain existing Bash CLI but fail accurately, preserve configuration and make the installed runtime/version visible. Linux systemd may remain optional; do not claim macOS reboot persistence until a supported launchd path exists.
- Every path finishes by opening/showing the same browser onboarding, with a clear copyable owner bootstrap step. Keep provider keys out of logs and generated instructions beyond explicit copy actions.

### Vercel evidence from this checkout

- `website/astro.config.mjs:7` sets `site: https://hermes-router.vercel.app`.
- `website/package.json:8` builds with `astro build`; Starlight is the documentation framework. `website/README.md:42` says output is `dist`.
- No tracked `vercel.json`, Vercel project metadata, or Vercel deploy workflow was found. The only tracked workflow is router smoke tests. This does **not** prove the project has no Git integration or production deployment; those are account state outside this audit.
- The Vercel site is documentation/marketing, not the long-lived Flask routing service. Preserve that boundary unless a separately justified hosted architecture is approved.
- Homepage currently leads with a Bash pipe-to-shell command (`website/src/content/docs/index.mdx:70`). Replace with platform-specific install choices and a primary “Get connected” path. Preserve explicit demo labels on sample dashboard numbers (`index.mdx:109`); replace with real screenshots only when the redesigned UI exists.
- Replace the Starlight starter README with actual development/build/publish instructions. Add a website build to CI, and a documented verified deployment target before publishing.
- Documentation is duplicated under `documentation/` and `website/src/content/docs/`. Establish one maintained source and a small deterministic publication/sync check, rather than editing both sets inconsistently.
- Correct existing stale extension configuration instructions (`documentation/deployment.md:226`, `vscode-extension/README.md:13`) to match browser-owned configuration.

## Acceptance checks for implementation

1. Existing inference clients and proxy keys survive migration; explicit owner/admin access is recoverable; no unauthenticated bootstrap takeover.
2. An inference-only/team key cannot list other keys, read global logs/usage, alter config, restart, or operate Docker instances. Its own usage and inference scopes remain usable.
3. Create/revoke key tests verify effective access immediately and after restart; persistence failure leaves the previous valid config intact.
4. Browser flows for cloud/local/existing setup, skipped/failed test message, copied client settings, logout and reload; no real provider needed in CI.
5. Browser test with hostile model names remains inert and renders readable text; keyboard-only navigation and mobile layout checks cover main tasks.
6. Fake slow/failed instance response cannot block Home, polling never overlaps, hidden tabs stop polling, and return-to-tab refreshes once.
7. Restart-capable and incapable installs show truthful outcomes; ordinary app keys cannot invoke administration through the extension.
8. Installer stub tests cover missing Python/git, failed clone/pull/venv/pip, already-installed dependencies, directory paths with spaces and reruns preserving config.
9. Docker integration check recreates a container against the same volume and verifies provider/owner/app key configuration survives.
10. Build website and extension; check navigation/docs links and absence of obsolete setup instructions. Preview the actual Vercel build before production rollout.

These checks are proposed work, not tests run during this audit. Full OmniRoute parity needs the independent competitor inventory; this document describes the UX and installation foundation without asserting parity.

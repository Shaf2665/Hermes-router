# Foundation dashboard and client result

Date: 2026-11-02. Scope: approved foundation task 3. No router.py edits or commits were made by this worker. Controller owns dashboard loading, Python/API integration and packaging.

## Delivered

- Extracted the dashboard into `dashboard.html` with every existing provider key/rotation, model override/capability, feature/cache, instance and application-access control retained. Five primary pages: Home, Providers, Connect, Activity, Settings. Application access stays in advanced Settings; no Team-ready claim.
- Applied the approved teal/light/dark/system-font direction, visible keyboard focus, reduced-motion handling, inert/hidden login gate and a 360px layout. Home honors backend readiness rather than treating a running process or stored key as proof of working inference.
- Browser authentication exchanges the administrator credential for the backend HttpOnly session. Mutations send CSRF, raw administrator credentials are not retained, the old localStorage entry is removed, and logout clears ephemeral keys/data. Requests from previous sessions cannot repopulate a new session's panels.
- Connect creates a real application key through the API, reveals/copies it deliberately, sends an explicit bounded test completion using that application bearer key with cookies omitted, and shows actual model/provider/latency/result. Copy-ready curl, OpenAI Python and Hermes VS Code instructions use placeholders rather than embedding credentials. A user can forget the one-time key on the page.
- Polls only data belonging to the visible page and expanded settings. One refresh batch runs at a time, hidden documents stop scheduled polling, and successful panels render independently of failed panels. Failures show last successful data time or the fact that no data loaded. Administrative requests have a 20-second client timeout.
- Escaped provider/model/log/feature/access metadata and attribute/handler values; instance links accept only HTTP(S). Instance UI shows snapshot state and age, including unchecked/checking/stale. Mutation controls expose pending state and prevent duplicate clicks. Application key edit/revoke/create reflects immediate backend application; provider changes retain their restart-needed state. Restart 409/errors retain the banner and provide Windows/Docker host remedies.
- VS Code keeps the existing application-key setting for inference. Optional owner status and remote restart use a dedicated router-scoped SecretStorage credential command; no administrator setting was added. Updated extension manifest/README, `documentation/vscode-extension.md` and the website mirror with preview/release distinction and truthful restart behavior. Website YAML preserved.

## Verification

- `D:/Hermes-router/.venv/Scripts/python.exe -m pytest tests/test_dashboard_playwright.py -q`: **7 passed in 29.19s** on the final implementation. Isolated real Flask/Waitress router and local mock provider; no operator configuration or real upstream credentials used.
- Cases: application key rejected at owner login; session login/resume/logout and no persistent browser credential; generated application key used for a real completion with no owner cookie; app admin denial; hostile provider/model/feature/log/access labels; independent usage failure; five-page/mobile navigation; nonoverlapping and hidden-tab polling; pending provider restart and truthful restart rejection; instance registration/snapshot freshness; live application name editing and revocation; process alive but provider unavailable.
- `node --check` on extracted dashboard JavaScript: passed. Temporary check file removed.
- Extension `npm run compile`: passed. `npx tsc --noEmit`: passed.
- Extension `npm test`: **1 passed**. Isolated VS Code/transport mock verifies router-scoped SecretStorage read/write/delete, missing owner guidance, and unchanged application key for chat.
- Visually inspected Connect and 360px Home screenshots. Base text/muted/action palette contrast checks exceed 4.5:1 in both themes. Screenshot artifacts (ignored): `.pytest_cache/dashboard-connect.png`, `.pytest_cache/dashboard-settings.png`, `.pytest_cache/dashboard-mobile.png`.

## Limits

No paid/live upstream provider, live VS Code/Copilot GUI, operating-system clipboard or Docker launch was exercised here. Existing chat stream/tool/cancellation code was preserved; its live client compatibility is not established by the SecretStorage wiring test. Provider changes may require an actual host restart before a first request succeeds. The extension preview has not been published to Marketplace or packaged/released as a VSIX by this worker.

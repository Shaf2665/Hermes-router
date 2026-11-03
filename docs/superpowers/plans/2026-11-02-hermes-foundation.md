# Hermes Foundation Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development with bounded file ownership and independent review. User approved implementation on 2026-11-02; do not request a second design/execution approval.

**Goal:** Deliver the foundation release and reproducible local/server setup, retaining the broader parity roadmap explicitly.
**Architecture:** Keep Flask/Waitress and existing routing. Separate browser assets, owner authentication and durable quota state only where needed. Independent agents own bounded router sections; controller integrates and reviews.
**Tech Stack:** Python 3.10+, Flask, requests, SQLite, vanilla browser UI, Bash/PowerShell, Astro, TypeScript.
**Spec:** docs/superpowers/specs/2026-11-02-hermes-upgrade-design.md (approved).

## Global Constraints

- Keep the existing inference endpoints/model ID and CLI compatible. No real credential/config edits or upstream calls during tests.
- Personal setup needs no external database or Node runtime. Team readiness requires owner separation, live revocation and durable usage.
- Read existing callers/tests before changes. No unrelated refactor, dependencies or placeholders claiming later parity.
- Use isolated tests, not the operator's environment. Keep graphify-out untouched.
- Existing assistant-owned branch is the work area; do not reset or overwrite other work. Controller owns commits to avoid shared-router staging races.

## Review Focus

1. App key attempts to use admin/instance/global telemetry APIs must fail; expired or cross-origin browser sessions must fail.
2. Revocation after minting must apply without restart; in-flight requests settle once and cannot reset allowances after restart.
3. Slow/offline instances must not delay list responses or healthy dashboard panels.
4. Failed/disconnected streams must close responses and must not double-charge usage or leak keys.
5. Clean install, failed install and container recreation must accurately preserve config and report readiness.

### Task 1: Owner access and durable usage

Files: router.py configuration/auth/key CRUD/KeyUsage/admission/accounting only; new focused auth/usage module if justified; tests/conftest.py, auth/usage regression tests, admin assertions in existing tests. Do not edit dashboard string, routing internals or instance probe helpers.

Interfaces: `ADMIN_API_KEY` generated and stored via `HR_ENV_FILE`; `POST /v1/admin/session` accepts `{api_key}` and returns `{csrf_token}` with HttpOnly SameSite cookie; `GET /v1/admin/session` returns authenticated session status/token; `DELETE /v1/admin/session` clears session. Cookie-authenticated mutations require `X-CSRF-Token`. Dedicated bearer admin calls also supported. `_auth_check()` gates inference; `_admin_check()` gates admin/global APIs, except app `/v1/usage` shows only caller data. Keys CRUD apply atomically live. `HR_USAGE_DB` controls SQLite quota database. Export helper/accounting contracts to controller/UI worker promptly.

- [x] Write failing tests for app denial/admin acceptance, session/CSRF/origin rejection, create/use/revoke, scope changes, durable counts and concurrent reservations.
- [x] Implement the smallest safe migration and owner bootstrap without logging secret values; preserve old inference keys.
- [x] Persist per-key usage/reservations, reject unknown-price budget routes, reconcile JSON and streamed usage exactly once; release failures and distinguish cached spend.
- [x] Test old instance/config cases with explicit admin auth and preserve inference tests with app auth.
- [x] Report tests, interface changes and any unresolved team-readiness blocker for integration review.

### Task 2: Routing responsiveness and instance snapshots

Files: router.py instance helpers, list_instances payload, upstream forwarding, route completion/streaming/cache, probe initialization only; focused tests and mock benchmark. Do not edit auth/session/key CRUD/KeyUsage or dashboard string.

Interfaces: list payload retains `instances[].live` and `docker`, adds freshness states. No live network/Docker I/O in list request; bounded deduplicated background probe refresh with deadlines. Keep `_instance_public(entry, include_live=True)` compatible. Coordinate streaming accounting hooks with Task 1 before altering endpoints.

- [x] Add failing tests proving instance list returns while a probe is blocked, duplicate probes are bounded, stale/deleted entries cannot overwrite current state, secrets omitted.
- [x] Implement snapshot cache keyed by configuration identity and bounded background probes, preserving mutation response contracts where possible.
- [x] Add response-close/cancellation/failover tests, fix failed-stream resource cleanup, bound cumulative routing attempts by deadline without changing public forward signature used by tests.
- [x] Correct semantic-cache compatibility and embedding identity, and remove the unused stats lock deadlock. Do not silently reuse old incompatible persisted embeddings.
- [x] Run targeted tests and repeated synthetic benchmark; document actual measurements without competitor claims.

### Task 3: Dashboard and client usability

Files: new `dashboard.html`, tests/test_dashboard_playwright.py and focused UI test support; vscode-extension/src and package metadata if required for owner/app separation. Do not edit router.py; controller replaces `_DASHBOARD_HTML` with reading dashboard.html after file is ready.

Interfaces: Task 1 session/CSRF endpoints; existing admin config endpoints; ordinary chat uses a newly generated app key, never the owner key. Retain all existing dashboard controls nested under Home/Providers/Connect/Activity/Settings and Team/access management. No exposed team-ready claims until backend gate passes.

- [x] Extract existing dashboard with controls intact, redesign navigable accessible personal flow using design tokens.
- [x] Implement session login/sign-out without localStorage secret, setup/connect page, explicit test-message action, independent visible-page polling with no overlaps and hidden-tab pause.
- [x] Escape all untrusted metadata and preserve keyboard/mobile usability, failures and pending states.
- [x] Update VS Code privileged connection to SecretStorage with dedicated admin credential; preserve existing inference configuration and streamed tools/cancellation.
- [x] Add browser tests for login, app/admin split, no persistent credential, malicious model label, visible polling, partial failure and connection test. Run compile.

### Task 4: Installation, packaging and guidance (controller)

Files: install.sh, get.sh, scripts/setup.sh/status.sh as needed, install.ps1/start.ps1, Dockerfile variants, docker-compose.yml, .env.example, .gitignore, documentation/setup.md and website mirror/navigation/landing page; focused install tests.

- [x] Make installers fail on clone/venv/dependency failures and preserve existing settings; add tested native PowerShell installer/start with no destructive global PATH edits.
- [x] Package new modules/assets in both images; compose persists state and binds port to loopback by default, needs no .env on fresh start.
- [x] Write exact local Windows/macOS/Linux and Linux-server Docker instructions, bootstrap/recovery, provider configuration, first request, SSH tunnel and optional HTTPS proxy, backup/update/rollback.
- [x] Align public site and docs with shipped features only; clarify unreleased branch requirements while changes remain unpushed.
- [x] Run installation syntax/failure tests, Python suite, browser suite, extension compile and Astro build; attempt Docker verification if daemon available.

### Task 5: Integration and review

- [x] Confirm all surfaces use agreed auth/config paths, baseline inference remains compatible, browser can complete first-request flow against mock provider.
- [x] Fresh independent review of code/security/packaging and spec compliance; resolve material findings.
- [x] Publish benchmark artifact and setup guide, update checked steps/remaining roadmap, commit coherent implementation.
- [x] Verify Vercel target/source and deploy tested site within existing authorization if tooling permits; report actual deployment status.

## Execution ledger

- 2026-11-02: Design approved by user. User requested native Windows 10/11 and Windows Server, native Linux, and Docker on Linux/Windows 10/11; all are covered in the setup guide.
- Ruling: implement approved foundation as a complete first release; later parity stages remain explicitly planned, never described as shipped.
- Ruling: use existing task branch with area ownership; no extra worktree or execution-method confirmation required for already authorized reversible work.

- Verification: 109 Python/browser tests passed; native Windows install/start succeeded; extension compile/typecheck/test/package and Astro build passed. Docker runtime unavailable; Compose validated.
- Website: Vercel preview dpl_GjSLaaJFNHqi2DMwkhsjQhhx9zAw READY; /setup/ returned 200 with all requested platform sections. Production alias remains unchanged.

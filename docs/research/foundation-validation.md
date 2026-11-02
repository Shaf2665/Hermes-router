# Foundation preview validation — 2026-11-02

This implements the approved foundation stage, not the full OmniRoute parity roadmap. Source changes are on `codex/hermes-upgrade-design`. No production router, provider account, Marketplace extension, or production website was changed.

## Delivered

- Home, Providers, Connect, Activity and Settings dashboard, with a simple personal setup flow and optional per-application access controls.
- Separate administrator authentication, expiring browser sessions, CSRF checks, immediate key revocation, model/provider scope and durable SQLite usage reservations.
- Instance snapshots with bounded background probes; upstream cleanup and routing deadlines; compatible semantic-cache identities; truthful provider readiness.
- Native Windows PowerShell installation/start, fail-fast Bash installation, persistent Docker configuration, safer system/user service setup and restart.
- Native Windows 10/11 and Windows Server, Docker Desktop on Windows 10/11, native Linux and Linux Docker instructions, including startup, first request, HTTPS/tunnel access and backup.
- Matching VS Code SecretStorage integration and packaged preview VSIX.

## Verification

- Final Python/browser suite: **109 passed in 40.59 seconds**. Command: `.venv/Scripts/python.exe -m pytest -q D:/Hermes-router/tests`.
- Seven Playwright browser tests use an isolated Flask router and mock upstream: sign-in, session resume, app/admin separation, first request, mobile navigation, malicious metadata, independent panel failure, polling, key revocation and readiness.
- Installation tests cover Bash failures, service ownership and failures, safe process targeting, and Windows PowerShell 5.1/pwsh success and failure paths.
- A separate **real native Windows installation** ran `install.ps1` in an isolated temporary source folder, installed requirements, launched `start.ps1`, and verified health, dashboard HTML and administrator session creation. Its own process tree was stopped afterward; no operator settings were used.
- VS Code: compile, TypeScript check, credential test and VSIX packaging passed. Artifact: `hermes-router-foundation-preview.vsix` (preview of the existing 0.9.5 package, not a Marketplace publication).
- Astro build passed: 16 pages plus search index. The existing missing-custom-404 entry warning remains; build succeeds with the generated fallback.
- All Bash scripts passed syntax checks; Python compilation, Compose configuration validation and `git diff --check` passed.
- Independent review resolved all material findings; see `foundation-review.md` and the six controller regression tests in `tests/test_foundation_review.py`.

## Performance evidence

The seven-sample, 50-instance mock benchmark measured a median 1042.023 ms for the former serial behavior, versus 2.417 ms for cold snapshots and 2.129 ms warm. This is instance-list responsiveness, not end-to-end model latency or a competitor comparison. Full method and samples are in `foundation-performance-result.md` and `foundation-instance-benchmark.json`.

## Boundaries

Docker CLI/Compose are installed but the Docker engine is unavailable here, so no image/container execution was verified. Actual Linux systemd and Windows Server Task Scheduler startup remain host verification steps. Live paid inference and provider billing were not exercised. Windows native inference was exercised against the browser suite's mock provider; the fresh install check had no real providers.

Money limits estimate spend, reject unsupported/unpriced routes, and cannot guarantee an invoice total. Token-limited keys reject Codex's unbounded subscription protocol. Live authorization assumes one router process; this is not multi-process synchronization, SSO, user invitations, or full team identity management. SQLite lifetime snapshots use aggregates but still scan that key's ledger rows. Requests deadlines bound retries/inactivity, not every DNS or trickling-read stall.

Browser restart is limited to a verified CLI-managed standalone process. Foreground processes need manual stop/start; services and containers use their host manager. This prevents false success and unsafe process discovery.

Source archives exclude secrets, runtime state, virtual environments, node_modules and graphify output. Broader provider/API/media/memory/agent parity remains in the approved staged design.

## Documentation deployment

Vercel preview `dpl_GjSLaaJFNHqi2DMwkhsjQhhx9zAw` is READY. The live [setup page](https://hermes-router-79t4506wa-shafiqs-projects-c6996881.vercel.app/setup/) returned HTTP 200; native Linux, Windows Server, Windows 10/11, Docker and Task Scheduler sections were verified. Only website source/assets were uploaded. The production alias was not promoted.

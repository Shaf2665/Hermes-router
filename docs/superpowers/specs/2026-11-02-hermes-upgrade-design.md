# Hermes Router: simpler experience, measured performance, broader compatibility

Date: 2026-11-02. Status: proposed design for review; implementation has not started.
Repository inspected: `Shaf2665/Hermes-router`, commit `9d87915`.
Independent builder review completed: no important planning questions remain after clarifying the parity snapshot, budget semantics and team-readiness gate. User approval is pending.

## What the user asked for

Improve Hermes Router's performance and user experience to compete with OmniRoute, cover its features, provide clean installation, and update the Vercel website if needed. The user confirmed the audience is **both individuals and teams, with a simple personal mode**.

Success means a user can install Hermes, connect a provider, verify a real request, and connect their preferred client without learning gateway internals. Teams can share a gateway without granting applications administrator access. More advanced features must not make the first-run experience harder.

This is a product upgrade across several releases. A dashboard redesign alone does not satisfy the request. All competitor feature families remain in the parity inventory until implemented, explicitly accepted as optional, or excluded by a subsequent user decision. A roadmap entry is not a shipped capability.

The proposed finish line is the **self-hosted OmniRoute release/v3.8.53 source snapshot `a09660dce605723ba582723ce1f572cedd837753`**. Planning must enumerate its individual provider/auth adapters, strategies, endpoints, integrations and packages as acceptance rows, not just the broad groups below. Full parity requires the applicable behavior of every row to be implemented and tested or an explicit user-approved exception. Later `main`-branch additions are research leads, not a moving release requirement. Where a linked document came from `main`, verify its behavior against this pinned snapshot before adding it to the binding checklist. Lack of an external account is recorded as an unverified integration, not successful parity.

Managed OmniRoute Cloud is a separately operated service, not functionality delivered merely by cloning its self-hosted source. Hermes's corresponding optional hosting work is a deployable, documented operating model; running a paid service, buying infrastructure and setting up accounts requires a separately defined operating scope. This does not prevent delivering the full self-hosted capability target.

## Evidence and current state

Companion research:

- `docs/research/2026-11-02-omniroute.md`: dated competitor inventory, source links, discrepancies and licensing.
- `docs/research/2026-11-02-performance.md`: source-confirmed bottlenecks, baseline tests and measurement method.
- `docs/research/2026-11-02-experience.md`: dashboard, installation and documentation audit.

Hermes already has HTTP connection pooling, key rotation, model-aware routing, tool/vision filters, exact/semantic caching, Anthropic translation, a Codex adapter, local providers, provider breakers, budgets, a dashboard, Docker instances and VS Code integration. Reuse these rather than advertise them as new features.

Concrete problems found:

1. Dashboard refresh fetches five endpoints every five seconds. `/v1/instances` probes instances serially; unrelated panels wait on those checks.
2. Inference and administration use the same `_auth_check()`. Provider-scoped application keys still authorize administration and global telemetry.
3. Streaming proxy-key accounting records an input estimate before stream consumption; completion usage/cost needs reconciliation after consumption.
4. Error responses in streaming failover paths need explicit cleanup. Timeout across a cascade is not bounded by one request deadline.
5. Startup capability probes are sequential. Serving readiness and optional discovery need separate states.
6. A restart can be reported as requested even when its script is missing or unsupported on Windows.
7. Installation, native Windows support, Docker persistence and documentation need a single consistent supported path.
8. Provider model metadata enters dashboard HTML without appropriate escaping in one rendering path. Apply the existing escaping helpers or text-safe DOM APIs and test hostile model names. Key revocation also requires restart today; new access controls need immediate atomic revocation.

The isolated non-browser baseline has 21 passing tests. This does not demonstrate live upstream correctness or superiority over OmniRoute.

A synthetic instance-list experiment used two process-local 10ms mock checks per instance and three requests per case. Median responses were 21.11ms for one instance, 104.04ms for five and 207.81ms for ten. This confirms serial additive delay; it is not a production or competitor benchmark.

## Architecture decisions proposed by the builder

Retain Python, Flask/Waitress, the existing HTTP transport, the Bash CLI on supported platforms, and Astro/Starlight for the public site. No framework rewrite and no compulsory Redis, vector database, frontend development server or cloud account for personal use.

The router API and authenticated dashboard run together on the user's machine/server. The Vercel site remains public documentation and installation guidance; it never receives provider credentials. Reuse existing SQLite support for local persistence where it meets the requirement. A third-party cryptography dependency is justified only when implementing credential encryption; do not invent encryption.

Separate the dashboard template/static assets from `router.py` when implementing the redesigned UI, updating both Docker images and all installation paths in the same change. Keep routing extraction minimal: move code only where required for reliable tests, isolated settings or adding adapters.

Keep the current model ID, endpoint shapes, CLI commands and configuration readable during upgrades. Compatibility aliases remain valid. Additive API fields and stable errors are preferred; intentional authorization changes have an explicit migration.

## The experience users see

Default navigation: **Home, Providers, Connect, Activity, Settings**. Enabling team mode adds **Team**. Instances, model overrides, routing policies, compression, memory and integrations appear within relevant settings rather than as a long first-run sidebar.

The first-run flow:

1. **Connect a provider.** Choose a known provider, a local runtime, or an OpenAI-compatible endpoint. Explain which credential is required. Mask secrets and never place them in URLs.
2. **Check the connection.** A user-triggered tiny request verifies inference, not just `/health`. Show the actual provider/model, latency and a clear failure remedy. Mention any potential upstream charge before the test action. Do not auto-send prompts to new providers.
3. **Connect your tool.** Select Copilot, Codex, Claude Code, Cursor, Cline, an SDK, or another client. Show versioned, tested instructions with the endpoint and a newly created application key. Only offer formats verified for that client.

Returning users see readiness, the next useful action, recent request outcomes and usage. Advanced routing begins with **Balanced, Faster replies, Lower cost, Local first**. These are presets over real eligibility rules, not promises that override tool, vision, context or access constraints. Existing `:fast` behavior remains compatible.

Every error should identify the failed step and one next action: bad credentials, no compatible model, cooling down, exhausted budget, unreachable router, or unsupported operation. No generic success toasts for skipped restarts. Forms retain non-secret input after recoverable errors. Generated keys are shown once with a deliberate copy action.

### Visual direction

Reuse Hermes's circuit identity and teal accent. Proposed tokens: ink `#172021`, paper `#F5F7F7`, surface `#FFFFFF`, teal `#2B7074`, success `#276848`, caution `#8C590D`. Dark mode uses the existing `#101415` and `#1B2325` surfaces with `#58B9BD` accent. Verify contrast rather than assuming these combinations pass.

Use Segoe UI Variable/system sans for product text and Cascadia/Consolas only for commands, keys and numbers where useful. Left-aligned reading flow, comfortable 16px body text, limited card nesting, explicit action labels. The distinctive element is a compact request path showing client, selected provider, and fallback outcome. Everything else supports reading and action.

```text
Hermes                 Home  Providers  Connect  Activity  Settings

Ready to route                         [Test a request]
Your tool  ----  Hermes  ----  Selected provider
One useful next action, only when something needs attention

Recent activity                       Usage today
Outcome / model / duration             Requests / tokens / estimated cost
```

Keyboard operation, visible focus, accessible form errors, reduced-motion support, light/dark themes and a usable 360px layout are acceptance criteria. No fabricated live metrics, custom font downloads or animation required for functionality.

## Personal and team access

Team mode initially means a single organization sharing one self-hosted gateway, not a multi-tenant hosted SaaS. Roles are **owner/admin** and **application/member**. Named keys provide per-application attribution, provider/model scope and budgets; a key label is not proof of a human identity. SSO and a human account directory remain a later identity feature.

Fresh installs generate separate administrator credentials and application keys. Administration, provider credentials, instance control, global activity and global spend require admin authorization. An application key can call inference and see only its own permitted usage. Never expose another caller's prompts, outputs or key material through telemetry or caches.

Key creation, revocation and scope updates apply atomically without restarting inference. Revocation rejects new requests immediately; already-admitted streams complete under their original authorization and are accounted once. Explicitly cancelling an active stream is a separate operation. Restrictive changes invalidate affected cache eligibility. Local-only, provider exclusions and configured spending/access restrictions must never silently fall back to disallowed candidates.

Migration choice: introduce a dedicated admin credential while preserving existing application keys for inference. On upgrade, a local CLI action creates/displays the admin credential and explains the change; remote installations can pre-provision it through configuration. Old proxy keys stop authorizing administration once upgraded. There is no silent promotion of every old key to admin and no unauthenticated network setup route. Preserve a documented local recovery path and test the CLI, dashboard and VS Code transition together.

For the browser, exchange the admin credential for an expiring, HttpOnly, SameSite session with CSRF protection on mutations; use Secure cookies under HTTPS. Keep raw admin credentials out of persistent browser storage. CLI/API admin calls may continue to use the dedicated admin token. Loopback development is explicitly supported; remote team deployments require documented HTTPS termination.

Persist spend/usage needed for quotas. Reserve estimated tokens/cost atomically before concurrent requests, reconcile once on completion/disconnect, and release reservations on failure. Provider-reported values and fallback estimates must remain distinguishable. Unknown prices are **unknown**, not guaranteed free. Cache hits must not double-count upstream spend.

Money limits are enforced **estimated-spend admission controls**, not a guarantee about an upstream invoice. When a key has a money limit, reject candidates with unknown prices, unknown billable units or no enforceable output bound. Reserve the conservative known request ceiling, allow the request only if it fits, then reconcile using reported usage; if usage is missing, retain the conservative reservation rather than treating the request as free. Price changes and provider billing discrepancies remain visible limitations. There is no automatic bypass; the owner must explicitly change that key's budget policy. Persist reservations with recovery semantics so a restart cannot reset the day's allowance.

Stage 1 must complete administrator/application separation, immediate atomic key revocation, durable usage/reservations and caller-scoped telemetry before team mode is labeled ready or enabled for sharing. Stage 2 adds richer team governance; it does not defer these prerequisites. The stage-1 upgrade may be previewed personally while team readiness remains unavailable.

## Performance and correctness work

### Dashboard and instances

Replace overlapping `setInterval` refresh with a single scheduled refresh after completion. Poll only visible page data, pause when the tab is hidden, and refresh configuration on entry or mutation. A failing panel keeps its previous data with a timestamp; it does not erase healthy panels.

Instance listing returns stored configuration and the latest health snapshot immediately. Bounded background probes refresh snapshots; each has a deadline. The UI distinguishes unchecked, checking, healthy, stale and unreachable. Manual refresh requests a probe without spawning unbounded duplicate work. Credentials stay out of snapshots and logs.

### Routing and streams

Close failed upstream responses before another attempt. Close successful streams on completion, error and client disconnect. Account for usage at stream finalization exactly once. Do not hold shared locks during network calls or SQLite I/O unnecessarily.

Track time to first token separately from total stream duration, routing overhead and upstream latency. Preserve the healthy-provider preference while distinguishing key quota limits, model rejection and provider outages. Enforce a total configurable request budget with remaining time passed to subsequent attempts. Do not retry after emitting response content, replay tools, or claim upstream idempotency where it is unavailable.

Configured candidates can serve while bounded capability discovery proceeds. Unknown capability handling must remain explicit; strict tool loops still require confirmed support. Health/readiness reflects the difference between process alive and a usable provider.

### Measurement gate

Benchmark baseline and candidate on the same host, worker count, payloads and deterministic mock upstreams. Include success, delayed first byte, 429, 5xx, disconnect, tool/vision requests, cached responses, and slow registered instances. Test concurrency 1, 8 and 32; publish p50/p95/p99, throughput, errors and resource use, with repetitions and warmup stated.

Proposed acceptance thresholds (targets, not measured promises):

- Warm cached instance-list response p95 below 200ms with ten slow/unreachable mock instances; latency must not grow with their network timeout sum.
- No concurrent dashboard refresh loop and no instance probes from viewing Home. Zero scheduled polling while the tab is hidden.
- No more than 10% regression in successful baseline routing overhead; any claimed improvement must repeat across at least three runs.
- Stream cleanup, accounting, access separation and cache isolation pass correctness tests under concurrent load.
- A controlled OmniRoute comparison uses a pinned release and equivalent settings before any public claim of being faster. Do not compare cloud-model latency as if it were gateway overhead.

## Full capability roadmap

The dated competitor research is the detailed checklist. Track each capability as existing, incomplete, planned, implemented/tested, or unavailable; preserve source and test evidence. Provider count alone is not an acceptance criterion.

| Area | Hermes delivery direction | Stage |
| --- | --- | --- |
| Reliable routing, key rotation, failover, breakers | Improve existing scopes, deadlines, cleanup and diagnostics | 1 |
| Dashboard, connection test, setup, installation | Personal-first flow, team entry point, truthful operations | 1 |
| Team keys, access, budgets, usage | All team-readiness prerequisites in stage 1; richer governance afterward | 1–2 |
| Provider catalog and custom endpoints | Shared catalog with capability metadata, generic compatible providers, documented local runtimes | 2 |
| Routing aliases/combos and strategies | User presets plus advanced weighted/priority/least-used/latency/cost/context/cache/health/reset-aware policies with deterministic eligibility | 2 |
| OpenAI Chat, Anthropic, embeddings | Preserve existing supported contracts and improve conformance | 1–2 |
| Responses API and Gemini formats | Explicit streaming/tool/error compatibility suites, reject unsupported semantics clearly | 2 |
| Ollama ingress and model discovery | Distinguish using Ollama upstream from serving an Ollama-compatible client API; add conformance tests for the latter | 2–3 |
| Client integrations and VS Code | Versioned copy-ready profiles, connection validation, tool/result fidelity and cancellation | 1–2 |
| OAuth/subscriptions, API keys, local, keyless sources | Implement adapters with account support and provider-specific tests; count each tested path accurately | 2–3 |
| Cookie/browser sessions and proxy transports | Optional provider-specific adapters only when a supported integration can be maintained; never prerequisite for setup | 3 |
| Exact and semantic cache | Existing cache, versioned namespaces and model/dimension compatibility, hit/savings diagnostics | 1–2 |
| Compression and context control | Optional lossless steps first, then named lossy engines with quality/tool-call regression fixtures and per-route opt-in | 3 |
| Persistent conversation memory | Optional retention and deletion, per-caller isolation, local storage first; external vector storage optional | 3 |
| Audio, images, video, search, reranking/media APIs | Provider-capability adapters; separate endpoints and binary/async job tests | 3 |
| Files, batches, OCR, moderation, classification/segmentation | Durable jobs/files where needed, lifecycle and isolation tests; do not collapse these into generic chat passthrough | 3 |
| Admission queues, concurrency and quota pools | Bounded queue wait, fair per-key admission, real in-flight accounting and deduplicated shared quota pools | 2 |
| Credential storage and proxy/network settings | Supported encryption/key lifecycle, scoped outbound transports, no credential leakage through diagnostics | 2–3 |
| Guardrails, redaction, schema validation, evals | Request validation first; optional policies and reproducible quality/cost suites | 2–3 |
| MCP gateway/tools and A2A | Auth-scoped optional protocol adapters; clear separation between inference and executing tools | 3 |
| Plugins, flows and automation extensions | Documented restricted integration contracts after stable provider/routing contracts | 3–4 |
| ACP/cloud agents, skills, webhooks and external context | Explicit permissions, data retention and execution boundaries; separate optional integrations | 3–4 |
| Playgrounds, search tools and reasoning controls | First-message test first, then optional task-specific workspaces with measured usage and fidelity | 1–3 |
| Catalog Radar, traffic inspection and gamification | Advanced optional views; usefulness and costs evaluated independently from inference performance | 3–4 |
| Analytics, quotas, exports, logs and backups | Honest measured/estimated usage, bounded redacted logs, local export/restore and audited admin actions | 1–3 |
| Desktop/PWA, headless/CLI, ARM and Android/Termux | Reliable core/headless installs first; optional device packaging and platform tests afterward | 1–4 |
| Localization and client configuration automation | Preserve a simple English baseline, then supported locales and backed-up/dry-run client setup automation | 2–4 |
| Managed hosting, multi-node infrastructure, SSO | Separate optional deployment/identity projects; never required for personal mode | 4 |

Stage 1 is a complete, usable foundation release, not completion of the entire parity request. Stage 2 establishes coding-tool/protocol and routing breadth. Stage 3 expands optional capability families. Stage 4 packages enterprise/hosted/distributed use. Each stage has its own tested release; don't expose nonfunctional toggles for later stages.

## Installation, website and releases

Supported installation paths: Docker for a consistent managed environment; native Python with the Bash helper on Linux/macOS/WSL; a first-class PowerShell path for native Windows. Detect prerequisites, print the actual location/port, preserve existing configuration, provide uninstall/rollback instructions, and never claim success solely because a background process started.

Docker must persist credentials/configuration, quota data and optional cache at documented paths across recreation. The basic image must truthfully state which controls it supports. Do not grant Docker socket access just to make the default dashboard work. Separate registration/monitoring from permission to create or destroy containers.

The first run writes secrets with appropriate filesystem permissions, provides local admin recovery, and verifies both readiness and an explicit tiny user-authorized inference request. Update scripts must retain rollback/error handling. CI covers Python 3.10/3.12 and native Windows where supported, shell/PowerShell entry points, both Docker variants, website and extension builds.

Vercel inspection confirmed project `hermes-router` (`prj_seylMi5rW7cpM7SWBt4UsvU6uRUi`), Astro, Node 24.x, domain `hermes-router.vercel.app`, and a READY production deployment. The API summary did not establish Git linkage, root-directory or automatic deployment behavior; verify those before releasing.

Update the landing page to lead with installation and first success; use operating-system-specific instructions and an honest feature/capability reference. Keep the public documentation mirrors consistent. Fix stale extension button instructions. Publish a preview, validate navigation/mobile/accessibility and setup instructions, then release to the existing verified project within the user's website-update authorization. Never claim full parity, free unlimited usage or benchmark superiority before evidence exists.

## Change surface and test obligations

| Surface | Required companion changes |
| --- | --- |
| `router.py` routing, auth, stats, config, instances | Existing callers; new focused auth/stream/concurrency/failure tests; migration fixtures; backwards-compatible inference behavior |
| Dashboard template/assets | Browser tests for setup, partial failures, visibility-aware polling, permissions, copy actions, keyboard/mobile; both Docker COPY lists |
| `scripts/*.sh`, `hermes-router`, installation and new Windows entry point | Version/restart/doctor/update semantics, config paths, safe upgrades, shell/PowerShell checks |
| `Dockerfile`, `Dockerfile.cli`, `docker-compose.yml` | Persistence, readiness, image contents, clean-start/recreate tests and no accidental new privileges |
| `.env.example`, credential examples and config metadata | Defaults, secret handling, migration documentation; do not modify real local credentials |
| `vscode-extension/src/*`, manifest/README | Dedicated admin credential storage using VS Code SecretStorage for privileged controls; application-key chat remains compatible; compile and stream/tool tests |
| `documentation/*`, `website/src/content/docs/*`, styles/assets | Matching supported instructions, actual features and tested platform flows; Astro build and link checks |
| CI and release workflow | Existing tests plus meaningful new regressions, isolated mock performance suite, versioned compatibility evidence, rollback |

## Scope boundaries and open approval

No paid accounts, provider subscriptions, managed hosting purchases or upstream account credentials are assumed. Optional integrations that need them remain explicitly unverified until available. Documentation research is evidence of a feature claim, not proof its implementation works. Use independently written Hermes code; check the upstream license before any reuse.

The proposed architecture, personal/team model, migration and staged delivery are builder recommendations, not decisions already approved by the user. The decision requested is approval of this complete direction, particularly the dedicated admin credential migration. Implementation planning follows approval. No product behavior, installation files or production deployment has been changed during this design pass.

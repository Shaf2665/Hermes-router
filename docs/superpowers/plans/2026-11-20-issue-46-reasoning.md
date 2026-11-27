# Reasoning controls implementation plan

> **For agentic workers:** Use superpowers:executing-plans task by task.

**Goal:** Deliver the approved first slice of #46 with truthful capability reporting.
**Architecture:** Pure reasoning policy, existing transports and administration,
one candidate budget used in admission and forwarding. Preserve legacy Auto echo.
**Tech Stack:** Python/Flask, vanilla dashboard JavaScript, TypeScript extension.
**Spec:** ../specs/2026-11-20-issue-46-reasoning-design.md

## Global constraints

- No new dependencies, transcript store, deployment or publication.
- Canonical levels: auto, off, low, medium, high. Explicit unsupported controls fail.
- Preserve the approved legacy Auto privacy exception; strict controls fail closed.
- Work on the existing dedicated branch; preserve checkpoint 49ed781 separately.

## Review focus

- Provider setting edits during inference must not change an admitted request.
- Native controls must survive format conversion and reject conflicting fields.
- Strict budgets must not expand between preflight, reservation and forwarding.
- Raw/private continuation must not slip through streaming or adapter conversion.
- Cached responses must not bypass new settings or misreport effective policy.

### Task 1: Pure policy and provider mappings

Files: reasoning_policy.py; tests/test_reasoning_policy.py.
Interfaces: validate_config(value), request_control(payload, header), resolve(config,
control, provider, model), native_settings(provider, model, policy, payload, output).
Native result is a dict with requested, effective, fields, strict, supported modes.

- [x] Test validation, precedence, native conflicts and unknown/gateway models.
- [x] Observe missing module failure; implement exact verified model mappings.
- [x] Test manual budget insufficient output and private-state/tool restrictions.
- [x] Run policy tests; commit with the integrated backend in Task 2.

### Task 2: Router integration and persisted settings

Files: router.py, responses_api.py, context_policy.py, tests/test_reasoning_routing.py.
Consumes Task 1 functions. Produces /v1/config/reasoning and scope-filtered
/v1/reasoning capabilities; headers and request-log policy diagnostics.

- [x] Test native normalization before admission on all three endpoints.
- [x] Snapshot config, filter candidates, apply fresh settings per transport.
- [x] Share output ceiling calculation across forwarding/admission/context.
- [x] Isolate cache by settings/control; avoid caching strict requests initially
      so diagnostics and privacy cannot be detached from their actual candidate.
- [x] Test settings auth/CSRF/save failure, config edits, failover, raw-state
      rejection, truncation, budget enforcement and scope-filtered discovery.
- [x] Run policy, routing, echo, Responses, context, usage and Anthropic tests.
- [x] Commit backend and tests.

### Task 3: Dashboard, extension and documentation

Files: dashboard.html; vscode-extension/src/client.ts, lmProvider.ts, package.json,
tests/reasoning.test.cjs; tests/test_dashboard_playwright.py; .env.example;
documentation/reasoning.md; website/src/content/docs/reasoning.md; release notes.

- [x] Add global/provider/model controls using existing settings patterns.
- [x] Show supported modes, effective configuration and reported/unknown tokens.
- [x] Extension follows server by default; explicit Auto sends the header on
      discovery and inference, with a context refresh on preference changes.
- [x] Test omission versus explicit Auto and saved settings/error presentation.
- [x] Document API precedence, model mappings, native controls and privacy gap.
- [x] Run full Python tests, extension tests/typecheck/build, website build.
- [x] Commit client/UI/docs changes.

### Task 4: Review and delivery

- [x] Fresh whole-branch review; fix important findings with regression coverage.
- [x] Re-run affected tests; inspect diff/status; record results below.
- [x] Report the delivered slice and remaining private-continuation limitation.

## Execution ledger

- Design approved by user on 2026-11-20; proceed natively in this chat.
- Pre-flight: Task 2 consumes Task 1 pure policy; Task 3 consumes Task 2 HTTP APIs.
- Ruling: disable caching for strict policies in this first slice, preserving
  legacy Auto caches with a policy revision namespace. This avoids inventing a
  metadata persistence format solely for cached diagnostics.
- Ruling: retain existing dedicated branch rather than a redundant worktree.
- Review ruling: Codex subscription cannot enforce output ceilings. Strict requests
  with an explicit ceiling reject that candidate; effort-only unbounded requests
  retain existing unrestricted-key access. Its output bound is unknown, never a
  fictional 8192 tokens. Synchronized clients and capped keys continue excluding it.
- Review ruling: a strict 2xx response that is empty/malformed/truncated returns
  an explicit error and retains usage, rather than repeating potentially billable
  inference. HTTP/network/rate-limit failover remains. This narrows legacy empty
  response retry behavior deliberately; the documentation states the distinction.
- Review reproduced and fixed deadline SSE errors, contradictory native enabled/off
  controls, Codex terminal-event validation, and unavailable output ceilings.

- Task 1: complete — pure policy, verified mappings, precedence and native-control tests.
- Task 2: complete — settings, snapshots, routing/budgets, strict streams and private-state tests.
- Task 3: complete — dashboard browser save/failure check; all 16 extension tests, typecheck and compile; 19-page website build.
- Task 4: complete — independent review findings reproduced and fixed. Full Python/browser run: 376 passed, 15 platform skips; subsequent affected checks: 187 passed, then 84 passed including additional native-adapter and restart tests.
- The reviewer’s follow-up verification was unavailable due to its usage limit; the author verified the four fixes with regression tests.
- No live-provider inference, Docker release, Marketplace publication, push or deployment was performed. Issue #46 remains open for private continuation work.

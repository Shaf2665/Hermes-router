# Issue 46: provider-aware reasoning controls

Status: approved by the user on 2026-11-20; implementing the first slice.

Issue: https://github.com/Shaf2665/Hermes-router/issues/46

## Baseline and intent

Give applications and dashboard users one reasoning policy while preserving
provider-specific behavior, tool calls, and enforceable context/spend limits.
Do not implement persistent conversations (#47), a new agent runtime, or a
reasoning-text viewer.

Implementation branch: `codex/issue-46-reasoning-controls`, starting at current
main `9b3b693`. The previous local work is preserved separately as `49ed781`
on `codex/hermes-upgrade-design`. Do not merge that older branch wholesale:
main has newer context synchronization, Responses support, model switches,
and reasoning-echo fixes. Selectively reconcile remaining dashboard/documentation
work in a separate task; it is not a prerequisite for #46.

Verified current code:

- `router.py` has per-model reasoning probes, native Anthropic and Codex adapters,
  key scopes, admission reservations, and shared routing/failover.
- Strict context/coding-client requests already prohibit extra reasoning headroom
  in `forward`; `_output_bound` still computes its reserve independently.
- `context_policy.py` supplies confirmed capacities and client budget revisions.
- `responses_api.py` is stateless, drops provider-private reasoning input items,
  and currently does not normalize incoming reasoning effort into chat payloads.
- DeepSeek/Kimi-compatible chat paths preserve `reasoning_content` for tool loops.
  Destination compatibility is checked, but this alone does not establish the
  source provider of incoming reasoning history.
- Settings already have persisted context controls and administrator/CSRF checks.

## Approach

Use a small pure `reasoning_policy.py` module plus existing adapters and settings
patterns. Resolve policy once per request; derive a fresh native configuration
for each candidate. Keep transport, admission, and persistence in their current
modules. No new general adapter framework or dependency is needed for policy.

Alternatives considered: independent provider controls would duplicate precedence
and failover rules; a gateway-wide provider abstraction would substantially
expand this issue. A shared policy with existing transports is the recommended
implementation choice, not a claim of user approval of this document.

## Request and settings contract

Canonical override: `X-Hermes-Reasoning: auto|off|low|medium|high` on all three
inference endpoints. Reject malformed values before reservation or upstream use.
Also normalize supported native inputs: chat `reasoning_effort`, Responses
`reasoning.effort`, and Anthropic `thinking`/`output_config.effort`. Do not silently
discard native inputs or fabricate an exact effort from an arbitrary token budget.

Precedence: explicit canonical request > explicit native request > exact model
override > provider override > global default. Conflicting canonical/native
controls return 400, rather than guessing which native fields remain meaningful.
Native-only budgets or modes retain a typed provider-specific constraint and are
eligible only on matching transports/models; cross-provider reinterpretation is
not automatic. Native extensions such as `xhigh` are accepted only where verified.

`auto` preserves legacy behavior, including the existing Codex medium default;
it is an explicit escape from configured overrides. Explicit non-Auto policies
are strict: skip unsupported candidates and return `reasoning_not_supported`
(400) if none qualify. Do not silently lower effort. Exhaustion of otherwise
compatible upstreams keeps the existing availability error behavior.

Admin-only `POST /v1/config/reasoning` patches a validated JSON configuration:

```json
{"default":"auto","providers":{"gemini":"high"},"models":{"openai::o3":"low"}}
```

Null patch entries remove overrides. Persist atomically as `ROUTER_REASONING_CONFIG`
in the existing environment file; update runtime state only after a successful
write. Bound object sizes, validate known identifiers and levels, and reject
unknown fields. Invalid persisted config is an actionable startup error.
Use existing lock, admin authentication, and CSRF mechanisms.

## Capabilities and provider translation

Maintain an explicit, source-linked table keyed by actual endpoint contract and
verified model ID/family. A probe that observes reasoning does not prove support
for configurable effort or disabling it. Unknown models remain Auto-only.
Avoid treating a model hosted by a gateway as equivalent to its direct API.

- OpenAI chat: supported `reasoning_effort`; Codex/Responses transport: supported
  `reasoning.effort`. Off maps to `none` only on models documented to support it.
- Anthropic: distinguish manual budgets from adaptive thinking plus output effort.
  Manual low/medium/high presets are Hermes budget choices, displayed as such;
  the builder must record their exact values and supported model IDs in tests/docs.
  Validate sampling/tool-choice restrictions and signed thinking continuity before
  enabling any model for thinking tool loops. Unsupported combinations fail clearly.
- Gemini: use its existing OpenAI-compatible transport and documented effort
  mapping. Do not send native generateContent fields to a chat endpoint. Models
  which cannot disable thinking must not advertise Off.
- DeepSeek/Kimi: validate the specific model's toggle/effort interface, preserving
  required tool-loop continuity. Binary thinking support is not evidence for three
  effort levels. Report provider-native budget/mode as well as Hermes policy.
- Other gateways: Auto-only until their own documented interface and model support
  are verified. Tests must cover rejection as well as positive capability cases.

Provider docs checked on 2026-11-20 establish that effort is model dependent:

- https://developers.openai.com/api/docs/guides/reasoning
- https://platform.claude.com/docs/en/build-with-claude/extended-thinking
- https://aistudio.google.com/docs/openai#thinking
- https://api-docs.deepseek.com/guides/thinking_mode/
- https://platform.kimi.ai/docs/guide/use-reasoning-effort
- https://platform.kimi.ai/docs/guide/use-thinking-models

Exact supported model IDs, manual-budget presets, and native restrictions are
builder-owned choices to verify against these sources during implementation.
Never assume a similarly named model inherits an unverified contract.

## Routing, budgets, and cache

Snapshot settings and revision at admission. Normalize request controls before
cache lookup or budget reservation. Scope/model enablement restrictions still
apply before reasoning eligibility. Recompute native settings for every failover
candidate without mutating the caller's payload or carrying the previous
candidate's fields forward.

One candidate output-budget calculation must serve admission, context checks,
and forwarding. Include reasoning within total output, apply confirmed model and
provider ceilings, and reserve enough for the complete eligible pool. Synchronized
and strict coding clients never gain extra headroom after preflight. Preserve
legacy Auto reserves only where already supported and safe. An explicit manual
thinking budget that cannot fit fails as `reasoning_budget_exceeded`; do not
quietly shrink the requested policy or claim success with an empty answer.

Include normalized request constraints and settings/capability revision in exact
and semantic cache isolation. Configuration changes also invalidate synchronized
client discovery revisions when eligibility/budgets change. Cached diagnostics
must describe the response's actual policy, not whichever candidate happens to
be available now. Disable semantic reuse across different reasoning constraints.

Before response bytes are emitted, ordinary compatible failover remains possible.
After output begins, emit the endpoint's explicit stream error on truncation or
upstream failure and settle partial/uncertain usage conservatively; never restart
the answer on another provider inside the same stream.

## Reasoning continuity and privacy decision

There is a real compatibility tension in the issue: the current direct-compatible
chat API exposes provider-returned `reasoning_content` so clients can echo it,
while #46 asks for no raw reasoning in responses and unchanged existing callers.
All three promises cannot be made simultaneously with today's wire contract.

Recommended first release: retain the existing legacy Auto echo contract and
document this compatibility exception; never display or log its text.
For unified explicit controls, initially permit thinking tool loops only when
the transport can preserve provider-bound state without exposing raw text.
Otherwise reject that combination with `reasoning_continuity_unsupported` and
explain the supported legacy workflow. Do not advertise full #46 completion until
this gap is resolved. Supporting sealed client-carried continuation blocks would
require a separate reviewed cryptographic format and client support; do not build
an ad hoc encryption scheme or a transcript store as an incidental shortcut.

The boundary is explicit:

| Request | Initial-release behavior |
| --- | --- |
| Resolved policy Auto, no native reasoning constraint | Legacy compatibility, including the existing raw `reasoning_content` echo on supported chat destinations, with or without tools. |
| Explicit Auto, no native reasoning constraint | Same legacy behavior; overrides a non-Auto administrator default. |
| Omitted request control, non-Auto configured default/override | Unified strict behavior; no legacy exception. |
| Any explicit native reasoning constraint or non-Auto policy | Unified strict behavior, even if an administrator default is Auto. |

The legacy exception temporarily retains acceptance of unbound client-supplied
`reasoning_content` and existing destination-gated forwarding. Its origin cannot
be verified, so cross-provider privacy is NOT guaranteed for legacy echo. Document
this limitation prominently; do not claim the no-cross-provider-state acceptance
criterion is complete for the whole product. This is the proposed compatibility
tradeoff the user is approving, not a security guarantee derived from a model name.

For unified strict behavior, incoming provider-private state must never be
broadcast across compatible-looking providers. The initial slice rejects incoming
raw reasoning history and rejects candidates whose thinking tool loops require
unimplemented continuation support, before upstream use. Stateless text answers
can use these candidates but suppress raw reasoning in their outputs. Native
thinking/signature input that cannot safely round-trip also fails clearly; do not
silently strip it and continue. Safe continuation later needs an authenticated,
caller- and endpoint/model-bound envelope and explicit client support. Until then,
keep such tool combinations disabled. Ordinary portable tool history remains
eligible for models that do not need private continuation.

Existing echo tests remain legacy compatibility tests. New tests cover explicit
Auto, non-Auto admin defaults, no-tools responses, native-only constraints, and
rejection of attempts to move private state between two direct providers.

This compatibility exception and the resulting phased scope require user review;
the builder must not silently decide that preserving raw echo satisfies the
issue's no-raw-output acceptance criterion.

## UI, clients, and diagnostics

Reuse Settings form patterns and accessible status/error messages:

```text
Reasoning / Thinking   [Auto v] [Save]
Provider               [Gemini v]   Override [Inherit v]
Model                  [model v]    Override [Inherit v]
Supported              Auto, Low, Medium, High
Requested: High   Native configuration: ...   Reasoning tokens: reported / unknown
```

Expose only scope-filtered capabilities to app clients; global settings editing
stays admin-only. Include requested policy, effective native configuration,
provider/model and reported reasoning-token counts in existing diagnostics.
Use a finite policy enum for metrics; never labels containing message content,
raw reasoning, credentials, or unbounded user-supplied values. Unknown reasoning
usage is not zero. Response headers provide policy diagnostics without changing
OpenAI/Anthropic body schemas; completed-stream usage uses normal usage fields.

Add optional extension preference, defaulting to follow server (omit the header).
An explicit Auto selection sends Auto. Settings changes refresh model information
when eligible budgets change. CLI examples use the documented request/native
controls; do not modify native CLI saved sessions or claim process handoff.

## Reach and verification

Implementation reaches `router.py`, new `reasoning_policy.py`, `responses_api.py`,
`context_policy.py`, `dashboard.html`, extension client/provider/settings/tests,
`.env.example`, repository docs, website mirrors, and release notes. Update
fixtures for capabilities and policy revisions. No session schema or database
migration is needed.

Test policy validation/precedence; atomic save failure and restart; all adapter
positive/negative mappings; native-field conflicts; streaming/nonstreaming tools;
scope and disabled-model enforcement; failover and private-state isolation;
cache policy changes; strict context budgets and per-key token/spend admission;
truncated/empty answers, cancel/failure settlement; dashboard auth/CSRF/XSS; and
extension omission versus explicit Auto. Run existing reasoning-echo, Responses,
context-sync, Anthropic-tools, access/usage and routing regressions, then full
Python suite, extension tests/typecheck/build, and documentation build.

Mock-based verification cannot certify provider availability or paid-account
features. Record live smoke coverage separately and do not use real credentials
or incur billable requests merely to validate a design.

Baseline checks on 2026-11-20: 139 existing reasoning-echo, Responses, context-sync,
and Anthropic-tool tests passed on `9b3b693`. Before committing the older local
checkpoint, 59 targeted Python tests, 12 extension tests, TypeScript checking,
and the 17-page website build passed. These are baseline checks, not evidence
that this proposed feature has been implemented or validated.

## Delivery sequence

1. Review the privacy/legacy compatibility decision above and approve the design.
2. Write the implementation plan: policy/config, adapters and budgets, routing and
   privacy, dashboard/clients/docs, then integration verification.
3. Deliver the supported slice with explicit capability/error reporting. Track
   any unsupported continuity combinations as incomplete #46 acceptance criteria.
4. Do not close #46, publish an extension, deploy, or push a release implicitly.

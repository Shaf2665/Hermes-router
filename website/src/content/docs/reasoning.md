---
title: Reasoning / Thinking
description: Provider-aware effort controls and compatibility limits.
---

# Reasoning / Thinking controls

Source preview for issue #46. Use matching router and extension source builds;
this change does not update published Docker images or Marketplace packages.

In dashboard **Settings → Reasoning / Thinking**, select **Auto**, **Off**, **Low**,
**Medium** or **High**. Saves apply to new requests and persist in the router's
environment file. An in-flight request keeps its original settings.

**Auto** preserves existing provider defaults, including medium for the Codex
subscription adapter. **Off** is available only where disabling thinking is
documented. The other levels require verified model support; they do not mean
equal reasoning quality or equal token usage across providers.

## Request controls and precedence

Send `X-Hermes-Reasoning` on `/v1/chat/completions`, `/v1/responses`, or
`/v1/messages` (including `/claude/v1/messages`). Omit the header to inherit.
An explicit `auto` overrides administrator preferences and selects legacy behavior.

Precedence: explicit request control, exact model override, provider override,
global default. Supported native controls also count as explicit requests:
chat `reasoning_effort`, Responses `reasoning.effort`, and Anthropic
`thinking` / `output_config.effort`. Conflicting header and native settings return
400. Native budgets/adaptive modes stay bound to their compatible transport;
Hermes does not guess an effort level from an arbitrary budget. Responses
`reasoning.summary` (sent by Codex CLI) is accepted and ignored because Hermes never
returns reasoning summaries; unknown fields and nested thinking controls in
`extra_body` are not supported.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8319/v1", api_key="YOUR_APP_KEY")
answer = client.chat.completions.create(
    model="hermes-router",
    messages=[{"role": "user", "content": "Review this algorithm..."}],
    max_tokens=8192,
    extra_headers={"X-Hermes-Reasoning": "high"},
)
```

Responses clients can instead use `reasoning={"effort": "low"}`. Anthropic SDK
clients can use `extra_headers={"X-Hermes-Reasoning": "low"}` or a supported native
`thinking` configuration. Codex/Claude Code requests still pass through Hermes'
existing stateless adapters and strict output/context checks. This feature does
not resume native CLI sessions or execute tools.

In VS Code, **Hermes Router: Reasoning** defaults to `server` (omit the header).
`auto` explicitly selects legacy defaults; other values request strict controls.
Changing this preference refreshes the advertised eligible context budgets.
The application key is used for discovery and inference, not the admin credential.

## Initial verified model coverage

| Endpoint / model | Controls | Native mapping |
| --- | --- | --- |
| OpenAI o3, o3-mini, o4-mini, GPT-5 / mini / nano | Low, Medium, High | `reasoning_effort` |
| OpenAI GPT-5.1 | Off, Low, Medium, High | Off → `none` |
| OpenAI GPT-5.2 / GPT-5.4 | Off, Low, Medium, High; native `xhigh` | `reasoning_effort` |
| GPT-5.5 through supported OpenAI/Codex transport | Low, Medium, High; native `xhigh` | Transport-specific effort |
| Codex subscription, same verified IDs | Model-dependent levels above | `reasoning.effort`; Auto retains medium |
| Groq GPT-OSS 20B / 120B | Low, Medium, High | `reasoning_effort`, `include_reasoning=false` |
| Gemini 2.5 Flash / Flash-Lite | Off, Low, Medium, High | OpenAI-compatible effort |
| Gemini 2.5 Pro | Low, Medium, High | Cannot turn thinking off |
| Claude Sonnet 4 / 4.5 and Haiku 4.5, including dated IDs | Off, Low, Medium, High | Hermes manual budgets: 1,024 / 4,096 / 8,192 tokens |
| Claude Sonnet 4.6 / Opus 4.6 | Off, Low, Medium, High | Adaptive thinking + `output_config.effort` |
| Direct DeepSeek `deepseek-flash`, `deepseek-v4-pro` | Off, Low, High; native `max` | Thinking toggle + effort; Medium is not advertised as a distinct level |
| Direct Kimi K3, K3.1 Pro / Highspeed | Low, High; native `max` | Effort; always reasons |
| Direct Kimi K2.5 | Off, or native enabled thinking | Toggle only; no invented effort scale |

All models also offer legacy Auto. Other models and gateways are Auto-only until
their own endpoint/model contract is verified. A reasoning probe or a model name
containing “DeepSeek” is not evidence that a gateway accepts the direct API fields.
Only listed dated OpenAI o3/o4-mini/GPT-5 and Claude manual families are matched;
unknown aliases do not inherit support automatically.

Thinking tool loops on Gemini, Anthropic, DeepSeek and Kimi are intentionally
unavailable with explicit non-Off controls in this first slice. They require
provider-private continuation support that this release cannot safely preserve.
Use a supported portable-tool model, or deliberately choose legacy Auto.

## Output limits, failover and privacy

Reasoning shares the total output ceiling with the final answer. Strict controls
never add the legacy `REASONING_TOKEN_RESERVE`. Confirmed model/provider output
ceilings still apply. Anthropic manual budgets must be smaller than total output;
Gemini's native Low/Medium/High budgets (1,024 / 8,192 / 24,576) also require room
for an answer. Choose sufficient total output; Hermes will not silently reduce
the requested thinking policy. Additional reasoning can increase latency and cost.

The Codex **subscription** transport cannot enforce an output-token ceiling.
Effort-only requests without a ceiling can use it with unrestricted keys, but an
explicit reasoning request with `max_tokens` / `max_completion_tokens` skips that
candidate. It remains excluded from synchronized client budgets and per-key
token/spend caps. Use a bounded API provider when an output guarantee is required.

Every failover candidate is checked against the request's policy and key scopes.
Hermes does not send one provider's native fields to the next provider. Strict
reasoning requests bypass response/semantic caches in this slice. Auto caches
are isolated by the reasoning configuration revision.

Explicit controls reject private reasoning history and strip private reasoning
fields from responses, including streaming responses. Truncation, malformed
streams, and empty answers produce errors; Hermes does not start a different
answer after stream output begins. Interrupted strict streams retain conservative
usage reservations if actual usage is unavailable.

A successful upstream HTTP response with an empty, malformed or truncated answer
ends strict inference with an error instead of replaying potentially billable
work on another provider. Ordinary network, HTTP-error and rate-limit failover
still works. Codex strict responses must include a successful terminal completion
event; a partial stream ending at EOF is not success.

**Legacy Auto privacy exception:** direct-compatible DeepSeek/Kimi tool workflows
continue returning and accepting `reasoning_content` as before, including requests
without tools. Its source is not authenticated; legacy cross-provider privacy is
not guaranteed. Do not render or log that field. Explicit Auto selects this
exception even when the global default is non-Auto. Native constraints or a
non-Auto inherited preference select strict behavior instead. Full private-state
continuation is unfinished #46 work, not a claim of this release.

## Diagnostics and administration

Authenticated `GET /v1/reasoning` returns caller-visible model capabilities and
effective configured policies. It never returns upstream keys. Client-config
revisions change when reasoning configuration changes. The dashboard's Live Log
shows requested policy, native effective configuration and reported reasoning
tokens; unknown usage stays **unknown**. No reasoning text is logged or displayed.
Cached legacy responses have no inferred provider/effort diagnostic.

Successful uncached inference includes `X-Hermes-Reasoning-Requested`,
`X-Hermes-Reasoning-Effective` (JSON), `X-Provider`, and `X-Hermes-Model` headers.
Counts returned by a provider remain in its normal usage fields. Capability
support describes the request interface, not an upstream account-access guarantee.

An administrator can patch `POST /v1/config/reasoning`:

```json
{"default":"auto","providers":{"groq":"high"},"models":{"openai::o3":"low"}}
```

Use `null` to remove a provider/model override. Unknown identifiers and invalid
values fail validation. Browser mutations require the normal admin session and
CSRF token. Saves are atomic; failed persistence leaves active settings intact.
The persisted setting is `ROUTER_REASONING_CONFIG`; invalid startup JSON/config
fails rather than silently changing reasoning behavior.

Error codes: `invalid_reasoning_control`, `reasoning_not_supported`,
`reasoning_budget_exceeded`, `reasoning_continuity_unsupported` (400), and
`reasoning_incomplete` (502 or a terminal stream error). Ordinary upstream
exhaustion still uses the existing availability errors.

## Provider references

Checked 2026-11-20. Live provider requests were not used to certify these mappings.

- [OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning)
- [Groq reasoning](https://console.groq.com/docs/reasoning)
- [Gemini OpenAI-compatible thinking](https://aistudio.google.com/docs/openai#thinking)
- [Claude extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)
- [Claude thinking troubleshooting](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting)
- [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)
- [Kimi reasoning effort](https://platform.kimi.ai/docs/guide/use-reasoning-effort)
- [Kimi thinking models](https://platform.kimi.ai/docs/guide/use-thinking-models)

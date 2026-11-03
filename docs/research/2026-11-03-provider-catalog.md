# Provider preset coverage — 2026-11-03

## What changed

65 API-key presets, including 48 new OpenAI-compatible providers. The preset contains
an endpoint, account/key link, dated model suggestions and conservative access guidance.
No credentials were used to test these external services. A registry entry establishes
an integration shape, not working credentials, current pricing or inference quality.

The existing runtime adapters remain intact. Additional providers reuse the existing
OpenAI chat-completions path, model fallback, key rotation and usage accounting.
Automatic model expansion and replacement on failed probes are disabled for additions
by default to prevent silently replacing a selection with a paid model.

Model discovery is owner-only, opt-in, bounded to a fixed preset host, has time/size
limits, refuses redirects and never saves or returns the submitted credential. Upstream
error bodies are not returned. New provider setup requires explicit model selection.
Saving a key and model settings uses the existing separate endpoints; if the second
save fails, the UI reports the partial save and asks the owner to retry before restart.

## Evidence and provenance

Compared the official OmniRoute site’s linked repository, branch `release/v3.8.53`:

- https://www.omniroute.online/
- https://github.com/cheaper-inference/OmniRoute/blob/release/v3.8.53/open-sse/config/freeModelCatalog.data.ts
- https://github.com/cheaper-inference/OmniRoute/tree/release/v3.8.53/open-sse/config/providers/registry
- https://github.com/cheaper-inference/OmniRoute/tree/release/v3.8.53/src/shared/constants/providers
- https://github.com/cheaper-inference/OmniRoute/blob/release/v3.8.53/docs/reference/FREE_TIERS.md

Endpoint/model facts and account links were adapted from this MIT-licensed project;
no executor code was copied. Each preset records its evidence URL in `provider_catalog.py`.
Additional provider-owned checks:

- https://kilo.ai/docs/gateway — newer canonical endpoint overrides the older registry URL.
- https://kilo.ai/docs/gateway/authentication — Bearer keys and selected free models.
- https://docs.ollama.com/cloud — hosted API, key page, account-specific usage.
- https://docs.llm7.io/ — documented model listing and compatibility.

Other new entries are registry-backed, not independently account-verified. They are
marked “Listed free tier — verify” or “Trial / limited credits”, never guaranteed free.
Some new-account credits can expire or require a payment method. The catalog intentionally
does not claim a total free-token budget or assume that a live model list proves zero price.

## Additional key-based providers

`ai21`, `api_airforce`, `arcee_ai`, `baichuan`, `baidu`, `bazaarlink`, `blackbox`, `bluesminds`, `bytez`, `deepinfra`, `deepseek`, `doubao`, `fireworks`, `freemodel_dev`, `friendliai`, `hyperbolic`, `iflytek`, `inference_net`, `kilo_gateway`, `liquid`, `llm7`, `longcat`, `modelscope`, `monsterapi`, `morph`, `nebius`, `novita`, `nscale`, `ollama_cloud`, `pollinations`, `publicai`, `reka`, `scaleway`, `sensenova`, `stepfun`, `tencent`, `uncloseai`, `requesty`, `tokenharbor`, `ovhcloud`, `agnes`, `navy`, `ainative`, `aion`, `sealion`, `routeway`, `nara`, `xkiro`.

## Entries not implemented as key-only presets

| OmniRoute ID | Reason |
|---|---|
| `agentrouter` | Anthropic-format adapter required |
| `agnes-cn` | Regional endpoint not verified |
| `agy` | OAuth flow required |
| `aihorde` | Queue/anonymous authentication adapter requires verification |
| `cloudflare-ai` | Account-specific URL required |
| `coze` | OpenAI compatibility needs verification |
| `duckduckgo-web` | Browser adapter required |
| `glm-cn` | Regional endpoint not verified |
| `huggingchat` | Browser session adapter required |
| `kiro` | OAuth flow required |
| `muse-spark-web` | Browser session adapter required |
| `nlpcloud` | Nonstandard completion endpoint |
| `onomeo` | Key acquisition details not verified |
| `predibase` | Deployment-specific endpoint requires verification |
| `qoder` | Specialized adapter required |
| `sparkdesk` | Duplicate of iflytek |
| `t3-web` | Browser session adapter required |
| `vertex` | Project and cloud credentials required |

Existing aliases: `glm` maps to Hermes `zai`; `opencode-zen` maps to `opencode`. Regional/coding-plan endpoints are not assumed interchangeable.

## Validation

See the change’s test results: runtime wiring for every preset, persistent key/model configuration, administrator-only discovery, malformed/oversized catalog rejection, redirect refusal, no automatic paid-model substitution, and browser setup with multiple models. Live paid inference, every external account and regional availability remain untested.

Validation run: Windows full suite **127 passed, 13 skipped**; Linux non-browser suite
**123 passed, 8 skipped**. Website build passed (16 pages; existing custom-404 warning).
The CLI accepted a new provider alias and persisted a comma-separated model list in an
isolated container. Docker smoke checks cover health, admin/app authentication and
persistent configuration/usage across container recreation, including ARM64 emulation.

## Published Docker artifacts

- `shafiq735/hermes-router:providers-20261103` (alias `latest`):
  `sha256:86943b8c549de490c24136dbd91806e4097f548525536860bf1156b99bc74cec`
- `shafiq735/hermes-router:providers-20261103-cli` (alias `cli`):
  `sha256:8bf59f32e35bab087b8c172d730a45769107b120b3ae6debbc532643d04b62d6`

Both are Linux AMD64/ARM64 manifest lists. Previous `foundation-20261103` tags remain
available for rollback. The VS Code extension code is unchanged; version 0.9.6 opens
this router’s updated browser dashboard. Users must update/restart their router to
get the new preset catalog; updating the extension alone is insufficient.

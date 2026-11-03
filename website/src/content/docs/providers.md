---
title: "Providers"
description: "Supported cloud AI providers, local models, and Codex options for building a resilient Hermes Router provider pool."
head:
  - tag: title
    content: Supported AI Providers & Local Models | Hermes Router
---

hermes-router routes across a pool of providers. You only need **one** configured provider
to start. Add providers and keys to create more failover options. Multiple keys do not
necessarily add quota: many services enforce limits per project, organization, or account.

Add keys with `hr auth add <provider>` (see [configuration.md](/configuration/) for where
they're stored).

Want help choosing? See the [free model rankings](/free-model-rankings/) for a
quality-first comparison of the exact models Hermes Router uses by default.

Dashboard provider keys and model selections apply immediately, including model resets.
Active responses finish normally. Background capability checks may use a small amount of
provider quota. File-based CLI or environment changes still require a restart.

## Add a provider from the dashboard

Open **Providers**, choose a provider, click **Get API key**, sign in on the provider’s
website and copy your key. Paste it into Hermes, select one or more model checkboxes,
and click **Save provider**. Restart Hermes when prompted, then use **Connect** to test.
The base URL is already set; you do not need to type it.

**Refresh model list** asks that provider for its current catalog using the pasted key
(or a saved key). It does not save the key or send a chat prompt. If the provider cannot
list models, use the suggested models or enter model IDs separated by commas.
The suggestions are dated snapshots, not a guarantee of availability or price.
The live catalog may contain paid models. No model checkbox is selected for a new provider.

You can repeat this for many providers, multiple keys and multiple models. Saving an
existing key again is safe and lets you change the selected models. Provider credentials
and model changes still require a restart; application access keys are managed separately.

There are **65 API-key presets** (48 additions on 2026-11-03). Access labels distinguish
free-model offers, trial/credit allowances and paid subscriptions. For newly added
presets, Hermes does not automatically discover or substitute unselected models by
default. Opting into `<PROVIDER>_AUTO_DISCOVER_MODELS=1` can add paid models; leave that
advanced setting off when you want only your selected models.

A free account does **not** make every model free. Eligibility, regional checks, credit
expiry and billing rules belong to the provider. Confirm the current offer on its site.
Provider startup checks can send small requests to the selected model.

### Preset directory

Key links lead to the provider’s key console or sign-in/start page; you may need to
create an account before the API-key screen is available. The exact key-only workflow
covers the presets below, not browser-cookie or OAuth services.

| Provider ID | Provider | Access guidance | Get API key |
|---|---|---|---|
| `agnes` | Agnes AI | Listed free tier — verify | [Open provider](https://agnes-ai.com) |
| `ai21` | AI21 Labs | Trial / limited credits | [Open provider](https://www.ai21.com) |
| `ainative` | AINative Studio | Listed free tier — verify | [Open provider](https://ainative.studio) |
| `aion` | Aion Labs | Listed free tier — verify | [Open provider](https://www.aionlabs.ai) |
| `anthropic` | Anthropic | Paid / subscription | [Open provider](https://console.anthropic.com/settings/keys) |
| `api_airforce` | Api.airforce | Listed free tier — verify | [Open provider](https://api.airforce) |
| `arcee_ai` | Arcee AI | Listed free tier — verify | [Open provider](https://arcee.ai) |
| `baichuan` | Baichuan | Trial / limited credits | [Open provider](https://www.baichuan-ai.com/) |
| `baidu` | Baidu (ERNIE) | Listed free tier — verify | [Open provider](https://console.bce.baidu.com/qianfan/ais/console/apiKey) |
| `bazaarlink` | BazaarLink | Listed free tier — verify | [Open provider](https://bazaarlink.ai) |
| `blackbox` | Blackbox AI | Listed free tier — verify | [Open provider](https://blackbox.ai) |
| `bluesminds` | BluesMinds | Listed free tier — verify | [Open provider](https://www.bluesminds.com) |
| `bytez` | Bytez | Trial / limited credits | [Open provider](https://bytez.com) |
| `cerebras` | Cerebras | Trial / limited credits | [Open provider](https://cloud.cerebras.ai/) |
| `cohere` | Cohere | Trial / limited credits | [Open provider](https://dashboard.cohere.com/api-keys) |
| `deepinfra` | DeepInfra | Trial / limited credits | [Open provider](https://deepinfra.com/dash/api_keys) |
| `deepseek` | DeepSeek | Trial / limited credits | [Open provider](https://platform.deepseek.com/api_keys) |
| `doubao` | Doubao | Trial / limited credits | [Open provider](https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey) |
| `fireworks` | Fireworks AI | Trial / limited credits | [Open provider](https://app.fireworks.ai/settings/users/api-keys) |
| `freemodel_dev` | FreeModel.dev | Trial / limited credits | [Open provider](https://freemodel.dev) |
| `friendliai` | FriendliAI | Listed free tier — verify | [Open provider](https://friendli.ai) |
| `gemini` | Gemini | Free tier / selected models | [Open provider](https://aistudio.google.com/apikey) |
| `github_models` | GitHub Models | Free tier / selected models | [Open provider](https://github.com/settings/personal-access-tokens) |
| `groq` | Groq | Free tier / selected models | [Open provider](https://console.groq.com/keys) |
| `huggingface` | Hugging Face | Trial / limited credits | [Open provider](https://huggingface.co/settings/tokens) |
| `hyperbolic` | Hyperbolic | Trial / limited credits | [Open provider](https://hyperbolic.xyz) |
| `iflytek` | iFlytek Spark | Listed free tier — verify | [Open provider](https://xinghuo.xfyun.cn) |
| `inference_net` | Inference.net | Listed free tier — verify | [Open provider](https://inference.net) |
| `kilo_gateway` | Kilo Gateway | Free tier / selected models | [Open provider](https://app.kilo.ai/) |
| `kimi` | Kimi | Paid / subscription | [Open provider](https://www.kimi.com/code/console) |
| `liquid` | Liquid AI | Listed free tier — verify | [Open provider](https://liquid.ai) |
| `llm7` | LLM7.io | Listed free tier — verify | [Open provider](https://llm7.io) |
| `longcat` | LongCat AI | Trial / limited credits | [Open provider](https://longcat.chat/platform/docs) |
| `mistral` | Mistral | Free tier / selected models | [Open provider](https://console.mistral.ai/api-keys) |
| `modelscope` | ModelScope | Listed free tier — verify | [Open provider](https://modelscope.cn/my/myaccesstoken) |
| `monsterapi` | MonsterAPI | Trial / limited credits | [Open provider](https://monsterapi.ai) |
| `morph` | Morph | Listed free tier — verify | [Open provider](https://morphllm.com) |
| `naga` | Naga | Free tier / selected models | [Open provider](https://naga.ac/) |
| `nara` | NaraRouter | Listed free tier — verify | [Open provider](https://bynara.id) |
| `navy` | NavyAI | Listed free tier — verify | [Open provider](https://api.navy) |
| `nebius` | Nebius AI | Trial / limited credits | [Open provider](https://tokenfactory.nebius.com/) |
| `novita` | Novita AI | Trial / limited credits | [Open provider](https://novita.ai/settings/key-management) |
| `nscale` | nScale | Trial / limited credits | [Open provider](https://nscale.com) |
| `nvidia` | NVIDIA NIM | Trial / limited credits | [Open provider](https://build.nvidia.com/) |
| `ollama_cloud` | Ollama Cloud | Listed free tier — verify | [Open provider](https://ollama.com/settings/keys) |
| `openai` | OpenAI | Paid / subscription | [Open provider](https://platform.openai.com/api-keys) |
| `opencode` | OpenCode Zen | Free tier / selected models | [Open provider](https://opencode.ai/auth) |
| `opencode_go` | OpenCode Go | Paid / subscription | [Open provider](https://opencode.ai/auth) |
| `openrouter` | OpenRouter | Free tier / selected models | [Open provider](https://openrouter.ai/settings/keys) |
| `ovhcloud` | OVHcloud AI | Listed free tier — verify | [Open provider](https://www.ovhcloud.com) |
| `pollinations` | Pollinations AI | Listed free tier — verify | [Open provider](https://pollinations.ai) |
| `publicai` | PublicAI | Trial / limited credits | [Open provider](https://publicai.co) |
| `reka` | Reka | Listed free tier — verify | [Open provider](https://docs.reka.ai/chat/overview) |
| `requesty` | Requesty | Listed free tier — verify | [Open provider](https://requesty.ai) |
| `routeway` | Routeway | Listed free tier — verify | [Open provider](https://routeway.ai) |
| `sambanova` | SambaNova | Trial / limited credits | [Open provider](https://cloud.sambanova.ai/) |
| `scaleway` | Scaleway AI | Trial / limited credits | [Open provider](https://console.scaleway.com/iam/api-keys) |
| `sealion` | SEA-LION | Listed free tier — verify | [Open provider](https://sea-lion.ai) |
| `sensenova` | SenseNova | Trial / limited credits | [Open provider](https://platform.sensenova.cn) |
| `stepfun` | StepFun | Trial / limited credits | [Open provider](https://stepfun.com) |
| `tencent` | Tencent Hunyuan | Listed free tier — verify | [Open provider](https://console.cloud.tencent.com/hunyuan/api-key) |
| `tokenharbor` | Token Harbor | Listed free tier — verify | [Open provider](https://tokenharbor.ai) |
| `uncloseai` | UncloseAI | Listed free tier — verify | [Open provider](https://uncloseai.com) |
| `xkiro` | xKiro | Listed free tier — verify | [Open provider](https://xkiro.com) |
| `zai` | Z.ai | Free tier / selected models | [Open provider](https://z.ai/manage-apikey/apikey-list) |

New IDs also work with `hr auth add <provider>` and `hr model set <provider> model-a,model-b`.
Their environment names use uppercase IDs: for example, `KILO_GATEWAY_API_KEYS` and
`KILO_GATEWAY_MODEL`. Hyphenated aliases such as `kilo-gateway` work in the CLI.
The additional presets use fixed URLs; endpoint overrides are not accepted from the browser.

### Coverage limits

This is not complete OmniRoute provider parity. Cloudflare Workers AI and Vertex need
account/project information; Kiro and similar services need OAuth; browser-chat services
need separate adapters. Some other entries have unverified endpoint or key-acquisition
details. These are not offered as working key-only presets. The repository’s
`docs/research/2026-11-03-provider-catalog.md` records the full comparison and exclusions.

## Free and evaluation access

Provider plans, model catalogs, regions, and limits change independently of Hermes Router.
Treat this table as an access guide, then check the linked provider console for the limits
that apply to your account before relying on it.

| Provider | Access notes | Sign up / current limits |
|---|---|---|
| Gemini | Free tier for eligible models, projects, and regions; limits are model/project-specific | [AI Studio](https://aistudio.google.com) · [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) |
| OpenRouter | Zero-priced model variants; the no-credit plan currently has a shared daily request limit | [OpenRouter](https://openrouter.ai) · [free-model limits](https://openrouter.ai/docs/faq#how-are-rate-limits-calculated) |
| SambaNova | Developer access and model availability are account-specific | [SambaNova Cloud](https://cloud.sambanova.ai) |
| GitHub Models | Included, rate-limited API use for experiments and prototypes; paid usage is separate | [GitHub Models](https://github.com/marketplace/models) · [rate limits](https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models#rate-limits) |
| Cerebras | Trial/credit access; verify current eligibility, payment-method requirements and expiry | [Cerebras Cloud](https://cloud.cerebras.ai) |
| Groq | Free plan with model-specific organization limits | [Groq Console](https://console.groq.com) · [rate limits](https://console.groq.com/docs/rate-limits) |
| Mistral | Experiment access is intended for testing and learning; availability depends on the account | [Mistral Console](https://console.mistral.ai) |
| Cohere | Trial/evaluation keys are currently limited to 1,000 calls per month | [Cohere Dashboard](https://dashboard.cohere.com) · [limits](https://docs.cohere.com/v2/docs/rate-limits) |
| Z.ai (GLM) | Some models are zero-priced; verify the current catalog and account limits | [Z.ai](https://z.ai) |
| Naga AI | Promotional or free access may change; verify before configuring it | [Naga AI](https://naga.ac) |
| NVIDIA NIM | Hosted API trial access and limits depend on the account/model | [NVIDIA Build](https://build.nvidia.com) |
| Hugging Face | Monthly Inference Providers credit; the free-user amount is currently $0.10 and subject to change | [tokens](https://huggingface.co/settings/tokens) · [pricing](https://huggingface.co/docs/inference-providers/en/pricing) |

> **Hugging Face note:** one user token can access models currently served by Inference
> Providers through an OpenAI-compatible endpoint. The catalog and serving partners change,
> and only eligible routed requests use the monthly credit. The default model uses the
> `:cheapest` suffix; change it with `HUGGINGFACE_MODEL`.

## Paid providers

Add your existing API key; the router handles everything else.

| Provider | Default model | API keys |
|---|---|---|
| OpenAI | `gpt-4o-mini` | [platform.openai.com](https://platform.openai.com/api-keys) |
| Anthropic | `claude-haiku-4-5` | [console.anthropic.com](https://console.anthropic.com) |

> Anthropic's API uses a different wire format from OpenAI. hermes-router translates
> automatically — your app sends the same OpenAI-format request regardless of which
> provider handles it.

## Codex (ChatGPT subscription)

Codex lets you use your **ChatGPT subscription** (Plus/Pro/Go) for completions instead of a
pay-per-token API key. It doesn't use an API key — it authenticates with OAuth tokens, so
setup is different:

```bash
codex login            # one-time, with the official Codex CLI (opens browser / device flow)
hr auth import-codex    # copy the login into the router (reads ~/.codex/auth.json)
hr restart
```

The router stores the account under `codex_accounts` in `auth.json`, **refreshes the access
token automatically** before it expires, and translates your OpenAI-format requests to the
Codex **Responses API** transparently. Add several accounts (run `hr auth import-codex` after
logging into each) and pair with `hr mode sequential` to drain one account's quota before the
next. Override the model with `CODEX_MODEL` (default `gpt-5.5`).

> **Unofficial integration:** this subscription-token path is not an OpenAI API-key
> integration. Review the terms that apply to your account and use only accounts you control;
> OpenAI may change or stop supporting the underlying behavior.

## Kimi (Moonshot coding plan)

The **Kimi coding plan** (Moonshot) is a subscription, but — unlike Codex — it authenticates
with a normal **API key** (`sk-...`), not OAuth. Its endpoint is OpenAI-compatible, so it adds
like any other provider:

```bash
hr auth add kimi        # paste your Kimi/Moonshot key
hr restart
```

Defaults to `https://api.kimi.com/coding/v1` with model `kimi-for-coding`. Using the standard
Moonshot API instead of the coding plan? Point it elsewhere with `KIMI_BASE_URL`
(e.g. `https://api.moonshot.ai/v1`) and set `KIMI_MODEL` to a model like `kimi-k2-0905-preview`.
Get a key at [platform.kimi.ai](https://platform.kimi.ai) / [platform.moonshot.ai](https://platform.moonshot.ai).

## OpenCode (Zen + Go)

[OpenCode](https://opencode.ai) Zen is an OpenAI-compatible gateway of coding-tuned models —
including a rotating pool of models currently marked free. It's a normal API-key provider (no OAuth):
sign in at [opencode.ai](https://opencode.ai), copy your key from **API Keys**, then:

```bash
hr auth add opencode
hr restart
```

The default routes to free models (`deepseek-v4-flash-free`, `nemotron-3-ultra-free`,
`mimo-v2.5-free`, `north-mini-code-free`). Free promotions rotate — when one ends OpenCode
returns a model error and the router automatically **skips it and fails over** to the next.
Reach the premium models (Claude, GPT, Gemini, GLM, Kimi, Qwen…) by
setting `OPENCODE_MODEL`.

**OpenCode Go** is OpenCode's paid subscription tier — the *same* API key against a different
endpoint, no separate auth. Check [OpenCode's current Go plan](https://opencode.ai/docs/go/)
for pricing and limits, then enable Go billing on opencode.ai,
then add it as its own provider so it's only used once you've subscribed:

```bash
hr auth add opencode_go      # paste the same OpenCode key
hr restart
```

> **Only do this after you've actually enabled Go billing.** Adding an `opencode_go` key is the
> router's *only* signal that you've subscribed — it doesn't verify it. A key added without Go
> billing enabled will fail on every request with an auth error (the router backs off after
> repeated failures instead of retrying forever, but it will never succeed). If you haven't
> subscribed, skip this section — OpenCode Zen above already covers the free tier.

Defaults to `https://opencode.ai/zen/go/v1` with chat-completions-compatible models
`deepseek-v4-flash,kimi-k2.7-code,mimo-v2.5`; override with `OPENCODE_GO_MODEL`.
Models that OpenCode exposes only through `/v1/messages` are not compatible with this
provider adapter.

## Local models (Ollama / LM Studio / llama.cpp)

Run a model on your **own machine** and route to it — free, private, and fast, with the cloud
providers as automatic fallback. Any OpenAI-compatible local server works (Ollama, LM Studio,
llama.cpp's server, vLLM…). It's **keyless**, so there's nothing to add with `hr auth add` —
just point the router at it:

```bash
# e.g. with Ollama:  ollama serve  &&  ollama pull llama3.1
hr model set local llama3.1     # writes LOCAL_MODEL; enables the local provider
hr restart
```

Or set it directly in `.env`:

```
LOCAL_BASE_URL=http://localhost:11434/v1     # Ollama default (LM Studio: http://localhost:1234/v1)
LOCAL_MODEL=llama3.1                          # comma-separate for multi-model failover
# LOCAL_EMBED_MODEL=nomic-embed-text          # optional: also serve /v1/embeddings locally
```

The provider turns on as soon as `LOCAL_BASE_URL` or `LOCAL_MODEL` is set.

**Conversation mode** — send the model id **`hermes-router:fast`** and the router
prefers your local model for short/casual turns,
falling back to the cloud pool for heavier requests. Plain `hermes-router` keeps the normal
smart routing across every provider.

## Valid provider names

All IDs in the preset directory above are valid, alongside these original IDs and the keyless/OAuth integrations:

Use these names with `hr auth add`, `hr model set`, and the `<PROVIDER>_*` environment
variables:

`gemini`, `openrouter`, `sambanova`, `github_models`, `cerebras`, `groq`, `mistral`,
`cohere`, `zai`, `naga`, `nvidia`, `huggingface`, `kimi`, `opencode`, `opencode_go`, `openai`,
`anthropic`, `codex`, `local`.

## Per-provider capabilities

Each provider's model is probed at startup for **function-calling** and **reasoning**
support; results show up in `hr status` and `/v1/status`. See
[usage.md](/usage/) for how those affect tool routing, and
[configuration.md](/configuration/) for the override variables.

---

**Next:** [Configuration](/configuration/) — tune models, per-key budgets, caching, and every other setting.

# Providers

hermes-router routes across a pool of providers. You only need **one** configured provider
to start. Add providers and keys to create more failover options. Multiple keys do not
necessarily add quota: many services enforce limits per project, organization, or account.

Add keys with `hr auth add <provider>` (see [configuration.md](configuration.md) for where
they're stored).

Want help choosing? See the [free model rankings](free-model-rankings.md) for a
quality-first comparison of the exact models Hermes Router uses by default.

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
| Cerebras | Free developer access with model/account-specific limits | [Cerebras Cloud](https://cloud.cerebras.ai) |
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
Reach the premium models (Claude, GPT, Gemini, GLM, Kimi, Qwen…) by setting `OPENCODE_MODEL`.

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

**Conversation mode** — send the model id **`hermes-router:fast`** (or the header
`X-Hermes-Profile: fast`) and the router prefers your local model for short/casual turns,
falling back to the cloud pool for heavier requests. Plain `hermes-router` keeps the normal
smart routing across every provider.

## Valid provider names

Use these names with `hr auth add`, `hr model set`, and the `<PROVIDER>_*` environment
variables:

`gemini`, `openrouter`, `sambanova`, `github_models`, `cerebras`, `groq`, `mistral`,
`cohere`, `zai`, `naga`, `nvidia`, `huggingface`, `kimi`, `opencode`, `opencode_go`, `openai`,
`anthropic`, `codex`, `local`.

## Per-provider capabilities

Each provider's model is probed at startup for **function-calling** and **reasoning**
support; results show up in `hr status` and `/v1/status`. See
[usage.md](usage.md) for how those affect tool routing, and
[configuration.md](configuration.md) for the override variables.

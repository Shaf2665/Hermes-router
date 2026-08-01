---
title: "Architecture — How it works"
description: "The full picture: the request pipeline, credential pool, smart routing, failover, protocol translation (OpenAI/Anthropic/Codex), caching, and observability."
---

hermes-router is a single Python file (`router.py`) running a small Flask/Waitress server. It
accepts OpenAI- or Anthropic-format requests and forwards each one to the best available
provider in a pool, handling key rotation, failover, and format translation transparently.

## The request pipeline

Every request flows through the same pipeline:

```
  ┌──────────┐   OpenAI- or Anthropic-format    ┌─────────────────────────────────────┐
  │ Your app │ ───────────────────────────────► │            hermes-router            │
  └──────────┘   Bearer / x-api-key (PROXY key)  │                                     │
       ▲                                          │  1. Auth check (constant-time)      │
       │                                          │  2. Cache lookup (per-caller)       │
       │            OpenAI/Anthropic response     │  3. Rate the request 1–5            │
       └────────────────────────────────────────►│  4. Order providers by fit + health │
                                                  │  5. Try providers, rotate keys      │
                                                  └───────────────┬─────────────────────┘
                                                                  │ first one that succeeds
                                                  ┌───────────────▼─────────────────────┐
                                                  │ Gemini · OpenRouter · Groq · Mistral │
                                                  │ Cohere · NVIDIA · Codex · Kimi · more │
                                                  └──────────────────────────────────────┘
```

1. **Authenticate** — the caller's key is compared against `PROXY_API_KEYS` in constant time
   (`hmac.compare_digest`). Both `Authorization: Bearer` and Anthropic's `x-api-key` are accepted.
2. **Cache lookup** — identical requests can be served from an in-memory cache, namespaced by
   the calling key (see [Response cache](#response-cache)).
3. **Rate the request** — a 1–5 difficulty score is computed from length and content, with no
   extra API call.
4. **Order providers** — each model is scored 1–5 for capability; the router prefers the
   lowest configured cost tier whose rating meets the heuristic request score, skips unhealthy
   ones, and rotates among equally-good ties.
5. **Try and fail over** — it sends to the first provider, rotating keys; on a rate-limit or
   error it cascades to the next while candidates remain. Exhausting the pool returns `503`.

## The moving parts

### Credential pool

Every provider can hold many keys (from `auth.json` first, then `.env`). Keys are tracked in a
thread-safe pool with per-key cooldowns. A key that gets rate-limited (HTTP 429) is put on a
short cooldown and skipped until it recovers.

**Rotation modes** (set with `hr mode`, see [Configuration](/configuration/#key-rotation-mode)):

- `round-robin` *(default)* — spread requests evenly across all keys; they deplete together.
- `sequential` — drain one key fully until it rate-limits, then move to the next, keeping later
  keys/accounts fresh in reserve. Ideal for rationing many accounts.

**Concurrent selection.** Selection holds a short critical section around the deque rotation and
comparison so concurrent requests cannot corrupt rotation state. Each key's actual usage count
is tracked and exposed per provider in `/v1/status` (`keys[].requests`) and as a tooltip on the
web dashboard's key dots — so adding more keys to a provider and watching the count spread evenly
across them isn't just a design claim, it's directly observable.

**Multiple models per provider.** A provider's `<PROVIDER>_MODEL` can be a comma-separated
list. Because some provider limits are model-specific, cooldowns are tracked per **(key,
model)** pair: when one model hits a 429, the router fails over to the next model on the same
key before cascading to the next provider. This adds routing options across keys, models, and
providers; it does not bypass shared account, project, organization, token, or daily quotas.
Each listed model is also a first-class
routing candidate (see [Smart routing](#smart-routing) below), not just failover. See
[Configuration](/configuration/#multiple-models-per-provider).

### Smart routing

Requests are scored for difficulty and models for capability (both 1–5, lower = more capable).
The router prefers the lowest configured cost tier whose rating meets the heuristic score.
Tool requests prefer models confirmed to support function calling; if every capability is
unknown, the router tries the pool rather than rejecting early. Optional **fast routing**
(`FAST_ROUTE_THRESHOLD`) sends short requests to low-latency providers first.

**Per-model scoring.** When a provider lists several models, each **(provider, model)** pair is its
own routing candidate, scored on *its own* rating and tool/reasoning capability — not the primary's.
So with `GEMINI_MODEL=gemini-2.5-flash-lite,gemini-2.5-pro`, an easy turn goes to `flash-lite` while
a hard or tool-using turn can pick `gemini-2.5-pro`, instead of the extra models only being used for
rate-limit failover. Within equal ratings, a provider's models keep their **listed order**
(cheapest first). Tool/reasoning support is detected per model, so models of different classes can
safely share one list. Each model's capability shows in `/v1/status` under `model_caps`.

**Local models & conversation mode.** A model running on your own machine (Ollama / LM Studio /
llama.cpp) can join the pool as the `local` provider — free, private, fast (see
[Providers](/providers/#local-models-ollama--lm-studio--llamacpp)). Sending the model id
`hermes-router:fast` (or header `X-Hermes-Profile: fast`) makes the router prefer that local
model for short/casual turns, with the cloud providers as automatic fallback for heavier
requests.

### Failover & circuit breaker

If a provider errors or times out, the router cascades to the next automatically. A provider
that keeps failing health checks (network errors or 5xx — not rate-limits or bad requests) has
its **circuit breaker** tripped: it's pulled out of rotation for a cooldown, then re-probed
(half-open). Healthy providers are always preferred. Tunable via the `BREAKER_*` settings.

### Response cache

Identical requests can be served from an in-memory TTL+LRU cache, saving free-tier quota. Cache
entries are **namespaced by the caller's API key**, so two different `PROXY_API_KEYS` never share
a cached answer for the same prompt — safe to expose to multiple users. Disable with
`CACHE_TTL_SECONDS=0`.

**Persistent cache** (opt-in, `CACHE_PERSIST=1`): the cache is also mirrored to a SQLite file
(`CACHE_DB_PATH`, default `./cache.db`) so it **survives restarts** — the router keeps saving quota
on prompts it answered before a redeploy. The DB is a durable mirror of the in-memory LRU
(write-through on store, deleted on eviction), so it stays bounded by `CACHE_MAX_SIZE` — raise that
to persist more — and expired rows are pruned on startup. All DB access is fail-soft: an error
degrades to the in-memory cache without breaking a request.

Cache entries contain request material and generated responses. Protect `CACHE_DB_PATH` like
application data, and disable caching for workloads where retaining that data is inappropriate.

**Semantic cache** (opt-in, `SEMANTIC_CACHE=1`) goes a step further: on an exact-match miss it
embeds the prompt (reusing the router's own embeddings pipeline) and returns a cached answer
whose stored prompt is *similar* above `SEMANTIC_CACHE_THRESHOLD` (cosine). It's a bounded linear
scan over the LRU within the caller's namespace, and degrades gracefully to exact-match when no
embedding provider is available — so it adds savings without changing behavior when off.

### Per-key budgets & rate limits

Each `PROXY_API_KEYS` entry can carry a requests-per-minute ceiling and per-UTC-day request,
token, **and estimated-cost** budgets (set globally via `PROXY_LIMIT_*` or per key in `auth.json`
with `hr limit`). A caller over its limit gets a `429` with `Retry-After` *before* any provider is
contacted; live counters appear in `/v1/status`. Unset = unlimited, so single-user setups are
unaffected. These ceilings constrain usage, but every proxy key can also call configuration-write
endpoints, so keys are suitable only for trusted users/services. See
[Configuration](/configuration/#per-key-budgets--rate-limits).

**Cost awareness.** Spend is estimated from a built-in, manually maintained per-model price
table and surfaced per provider and per key in `/v1/usage`, `/v1/status`,
and `/metrics` — with an optional second currency (`COST_FX_RATE`). See
[Configuration](/configuration/#cost--spend-awareness).

These values are operational estimates, not provider invoices. Unknown models and providers
marked as free/subscription can show `$0` even when an upstream plan or promotion changes.

### Token estimation

Request text is encoded with `tiktoken` (`o200k_base`, loaded lazily) when available, with a
`characters ÷ 4` fallback. Counts remain estimates because provider tokenizers and chat-format
overhead differ.

### Capability probing

At startup the router probes each provider once to learn its real model, whether it supports
**function calling**, and whether it's a **reasoning model**. Results are cached to
`router_state.json` for `ROUTER_STATE_TTL_HOURS` (default 24h) so restarts don't re-probe. You
can override any result with `<PROVIDER>_SUPPORTS_TOOLS` / `<PROVIDER>_REASONING`.

Reasoning models spend output tokens on hidden chain-of-thought, so the router reserves extra
output budget (`REASONING_TOKEN_RESERVE`) to stop a small `max_tokens` from yielding an empty reply.

### Request guardrails

The router defends itself and avoids wasted upstream calls:

- **Body-size limit** — requests larger than `MAX_REQUEST_BYTES` (default 10 MB) are rejected
  with `413` before any provider is contacted, so a buggy client can't exhaust memory.
- **Large-payload skip** — some providers reject requests above model/account limits. When a
  request is estimated to exceed the configured provider ceiling
  (`<PROVIDER>_SKIP_TOKENS_OVER`), that provider is skipped and the router cascades on instead of
  burning a guaranteed-failed attempt.
- **Output clamp** — providers that `400` when `max_tokens` exceeds their output cap have the
  requested output transparently clamped down to their ceiling (`<PROVIDER>_MAX_OUTPUT_TOKENS`),
  so the call still succeeds.

### Concurrency

The server runs on Waitress with a configurable thread pool (`WORKER_THREADS`, default 16). The
upstream HTTP connection pool scales with that automatically, and streaming responses close their
upstream connection cleanly when the stream ends or the client disconnects.

## Protocol translation

Your app always speaks one format; the router adapts to whatever the chosen provider needs.

| Provider type | Wire format | How the router handles it |
|---|---|---|
| Most providers | OpenAI Chat Completions | Pass-through (the router's native format) |
| Anthropic | Messages API (`/v1/messages`) | Two-way translation incl. tools & streaming |
| Codex (ChatGPT) | **Responses API** over OAuth | Two-way translation + OAuth token lifecycle |

- **OpenAI ⇄ Anthropic** — `/v1/messages` is accepted for Anthropic-SDK apps, translated to
  OpenAI format, routed through the same pipeline, and translated back (including `tool_use` /
  `tool_result` blocks and streaming).
- **Codex (ChatGPT subscription)** — authenticates with OAuth, not an API key. Accounts are
  imported with `hr auth import-codex`; the router mints fresh access tokens from the refresh
  token, sends requests to the ChatGPT backend in Responses-API format, and translates the SSE
  stream back to OpenAI chunks. Multiple accounts pool naturally and pair with `sequential`
  rotation to ration them. See [Providers](/providers/#codex-chatgpt-subscription).

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | proxy key | OpenAI chat completions (streaming + tools) |
| `POST /v1/messages` | proxy key | Anthropic Messages API (translated) |
| `POST /v1/embeddings` | proxy key | OpenAI embeddings (stable provider order) |
| `GET /v1/models` | proxy key | Advertises the `hermes-router` model id |
| `GET /v1/status` | proxy key | Per-provider health, latency, keys, rotation, cache |
| `GET /health` | none | Liveness check for uptime monitors |
| `GET /metrics` | optional | Prometheus metrics (set `METRICS_REQUIRE_AUTH=1` to lock) |

## Observability

`hr status` renders a live dashboard (provider health, latency, key cooldowns, cache, rotation
mode) from `/v1/status`. `/metrics` exposes Prometheus counters and gauges for Grafana — counts
and timings only, never request content. See [Monitoring](/monitoring/).

## Ways to run and connect

The same `router.py` engine runs everywhere; you choose how to launch it and how to drive it.

**Run it:**

- **`hr` CLI** *(Linux/macOS/WSL)* — `hr setup`, `hr auth add`, `hr status`, `hr restart`. The
  friendly day-to-day way to manage a local router. See [Deployment](/deployment/#path-2--linux--macos-the-hr-way).
- **Docker image** — the prebuilt multi-arch [`shafiq735/hermes-router`](https://hub.docker.com/r/shafiq735/hermes-router)
  runs the same on Windows, macOS, and Linux: `docker run -p 8319:8319 …`. See [Deployment](/deployment/#path-1--docker-easiest-any-os).
- **Hugging Face Space** — host it on a Space; plan eligibility and sleeping behavior depend
  on the selected hardware/account. See [Deployment](/deployment/#path-4--hugging-face-space-host-it-online).

**Connect to it:**

- **Compatible OpenAI or Anthropic clients** — the supported endpoint subset is listed in
  [Usage](/usage/).
- **VS Code extension** — monitor the provider pool, manage the router, *and* use hermes-router
  as a model inside Copilot Chat (including agent mode). See [VS Code Extension](/vscode-extension/).

## Design principles

- **Self-contained** — one Python file; keys live in your own `auth.json` (git-ignored, `0600`).
  Nothing is installed system-wide beyond the `hr` symlink.
- **Configured by environment** — every behavior is an env var with a sensible default; see
  [Configuration](/configuration/).
- **Core vs. add-ons** — a small set of **core** features is always on (auth, failover, smart
  routing, the circuit breaker…); everything optional is an **add-on** you toggle with
  [`hr features`](/configuration/#hr-features--see-and-toggle-add-ons). Add-ons default to off
  (so a fresh install is minimal) and never change core behavior.
- **Fail soft** — when in doubt the router makes forward progress (e.g. if every provider's
  breaker is open it probes them all) rather than hard-failing while options remain.

---

**Next:** [VS Code Extension](/vscode-extension/) — monitor and manage the router, and use it as a model inside Copilot Chat.

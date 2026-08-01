# Configuration

All configuration is via environment variables (in `.env`) and the `auth.json` credential
store. Most settings are optional; the router can serve model requests once at least one
provider (including a local model) is configured.

## Core features vs. add-ons

hermes-router splits its behavior into two groups:

- **Core features** — always on; they *are* the router. Auth, the credential pool + key
  rotation, failover, the circuit breaker, smart routing, protocol translation
  (OpenAI/Anthropic/Codex), capability probing, token estimation, request guardrails, and
  usage/estimated-cost tracking.
- **Add-ons** — optional behaviors you turn on when you want them. Each is backed by an
  environment variable (or some `auth.json` config), and unset = off (except the response
  cache, on by default).

### `hr features` — see and toggle add-ons

```bash
hr features list                      # core features + every add-on, with on/off
hr features enable persistent_cache   # writes the backing var to .env
hr features disable semantic_cache
hr restart                            # apply
```

| Add-on | Backing setting | Default | What it does |
|---|---|---|---|
| `response_cache` | `CACHE_TTL_SECONDS` | **on** | Serve identical requests from an in-memory TTL+LRU cache |
| `semantic_cache` | `SEMANTIC_CACHE` | off | Also serve cached answers for *similar* prompts |
| `persistent_cache` | `CACHE_PERSIST` | off | Mirror the cache to SQLite so it survives restarts |
| `fast_routing` | `FAST_ROUTE_THRESHOLD` | off | Short requests prefer low-latency providers on ties |
| `model_discovery` | `AUTO_DISCOVER_MODELS` | off | Refresh provider model lists from `/models` at startup |
| `metrics_auth` | `METRICS_REQUIRE_AUTH` | off | Require the proxy key on `/metrics` |
| `cost_currency` | `COST_FX_RATE` | off | Show a second currency (e.g. ₹) alongside USD spend |
| `key_budgets` | `auth.json` / `PROXY_LIMIT_*` | off | Per-key RPM / daily request / token / cost ceilings — manage with `hr limit` |
| `local_model` | `LOCAL_BASE_URL` / `LOCAL_MODEL` | off | Route to a model on your own machine — manage with `hr model set local` |

`hr features enable/disable` toggles the simple **flag** add-ons by writing their variable to
`.env`. The last two are richer config, so `hr features` shows their status and points you to
the command that manages them. The live state is also in `/v1/status` under `features`.

## Where your keys live

`hr auth add` writes to **`auth.json`** — the router's own credential store, kept next to
the router. The repository ignores this file; do not force-add or publish it. Codex (ChatGPT
subscription) logins are stored separately under `codex_accounts` (via
`hr auth import-codex`); the router refreshes their OAuth access tokens automatically.

```json
{
  "providers": {
    "openrouter": ["sk-or-key1", "sk-or-key2"],
    "gemini": ["AIzaSy-key"]
  }
}
```

> Keys in `.env` (e.g. `OPENROUTER_API_KEYS=k1,k2`) still work too — the router reads
> `auth.json` first, then falls back to `.env`. Point at a different file with
> `ROUTER_AUTH_FILE=/path/to/auth.json`.

## Settings (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8319` | Port to listen on |
| `HOST` | `0.0.0.0` | Bind address. Set `127.0.0.1` to listen on localhost only (recommended on a shared/VPS host — reach it via localhost or an SSH tunnel). Keep `0.0.0.0` for Docker. |
| `PROXY_API_KEYS` | *(auto-generated)* | Comma-separated keys your app uses to authenticate — and the key needed to open the web dashboard. If left unset (or on the `.env.example` placeholder), the router generates a real random key on first boot and saves it back to `.env`, logging it once. Add more from the dashboard's **Access Keys** page, or set your own here. |
| `ROUTER_AUTH_FILE` | `./auth.json` | Where keys are stored |
| `HERMES_INSTANCES_FILE` | `./instances.json` | Where the dashboard's Instances registry is stored. It may contain generated instance proxy keys and copied provider keys, so keep it private and never commit it. |
| `CACHE_TTL_SECONDS` | `300` | Response cache lifetime (`0` disables). Entries are namespaced per API key, so different `PROXY_API_KEYS` do not share a cached answer |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `METRICS_REQUIRE_AUTH` | `0` | Require the proxy key on `/metrics` (`1` to enable) |
| `REASONING_TOKEN_RESERVE` | `4096` | Extra output budget added for reasoning models so hidden chain-of-thought doesn't eat the answer (`0` disables) |
| `ROTATION_MODE` | `round-robin` | How keys are picked within a provider (set in the dashboard or via `hr mode`) — `round-robin` or `sequential` |

### Advanced settings

Sensible defaults — most users never touch these.

| Variable | Default | Purpose |
|---|---|---|
| `MAX_REQUEST_BYTES` | `10485760` (10 MB) | Max request body size; larger requests get `413` (guards against memory exhaustion) |
| `WORKER_THREADS` | `16` | Waitress worker threads (concurrency). The HTTP connection pool scales with this |
| `CACHE_MAX_SIZE` | `100` | Max entries in the response cache (LRU eviction) |
| `CACHE_PERSIST` | `0` | If `1`, mirror the cache to a SQLite file so it survives restarts (opt-in). The DB mirrors the in-memory LRU, so it stays bounded by `CACHE_MAX_SIZE` — raise that to persist more |
| `CACHE_DB_PATH` | `./cache.db` | SQLite file for the persistent cache. On read-only hosts (e.g. HF Spaces) point it at `/tmp/cache.db` |
| `SEMANTIC_CACHE` | `0` | If `1`, also serve cached answers for *similar* prompts (needs an embedding provider; falls back to exact match otherwise) |
| `SEMANTIC_CACHE_THRESHOLD` | `0.95` | Cosine-similarity cutoff for a semantic hit (`1.0` = identical; lower = looser matching) |
| `FAST_ROUTE_THRESHOLD` | `0` | If >0, requests under this many tokens prefer low-latency providers first (`0` disables) |
| `AUTO_DISCOVER_MODELS` | `0` | If `1`, fetch configured providers' `/models` lists at startup, prune listed models that disappeared, and append the best discovered models |
| `AUTO_DISCOVER_MODEL_LIMIT` | `8` | Max models kept per provider when `AUTO_DISCOVER_MODELS=1` |
| `{PROVIDER}_EXCLUDE_MODELS` | — | Comma-separated model IDs to block for a provider (case-insensitive). Excluded models are stripped from config and discovery, e.g. `OPENROUTER_EXCLUDE_MODELS=some/model:free` |
| `ROUTER_MODEL_ID` | `hermes-router` | The model name clients send (the router maps it to each provider's real model) |
| `ROUTER_STATE_FILE` | `./router_state.json` | Where provider ratings/capabilities are cached between restarts (use `/tmp/...` on read-only hosts like HF Spaces) |
| `ROUTER_STATE_TTL_HOURS` | `24` | How long the cached probe state is trusted before re-probing (`0` = re-probe every start) |
| `REQUEST_LOG_SIZE` | `500` | Maximum in-memory request-metadata entries (`0` disables the request log) |
| `BREAKER_WINDOW` | `8` | Recent outcomes the circuit breaker weighs per provider |
| `BREAKER_MIN_SAMPLES` | `4` | Minimum samples before the breaker can trip |
| `BREAKER_ERROR_RATE` | `0.5` | Health-failure fraction that trips the breaker |
| `BREAKER_COOLDOWN` | `60` | Seconds the breaker stays open before re-probing |

Response-cache entries contain request material and generated responses. The in-memory cache
retains them until expiry/eviction; `CACHE_PERSIST=1` also writes them to `CACHE_DB_PATH`.
Protect that file and disable caching where this retention is inappropriate.

### Instance manager settings

The web dashboard's **Instances** page stores its registry in `HERMES_INSTANCES_FILE`. A registry
entry can be either:

- **external** — a name, base URL, and optional proxy key for a router you started elsewhere
- **docker** — a Docker image, host/container port mapping, generated or supplied proxy key, and
  env vars for a managed container

The file is written as JSON with `0600` permissions where possible and is listed in `.gitignore`.
Treat it like `auth.json`: it can include secrets.

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_INSTANCES_FILE` | `./instances.json` | Registry file for monitored and Docker-managed instances |
| `HERMES_INSTANCE_IMAGE` | `hermes-router:latest` | Docker image used when the dashboard launches a managed instance |
| `HERMES_INSTANCE_CONTAINER_PORT` | `8319` | Port inside the managed container |
| `HERMES_INSTANCE_DOCKER_PREFIX` | `hermes-router` | Prefix for generated Docker container names |

When creating a Docker instance, the dashboard can copy selected existing provider keys from the
manager router into the child container. The browser only receives provider names and key counts;
the backend reads the real keys server-side from `auth.json` and `.env`, then writes the matching
container env vars, for example:

| Provider | Env var copied into the instance |
|---|---|
| `gemini` | `GEMINI_API_KEYS` |
| `openrouter` | `OPENROUTER_API_KEYS` |
| `github_models` | `GITHUB_MODELS_TOKENS` |
| `openai` | `OPENAI_API_KEYS` |
| `anthropic` | `ANTHROPIC_API_KEYS` |

Manual env vars entered in the dashboard's Docker settings are merged with copied keys. If both
set the same env var, the copied provider keys win for that provider.

### Per-key budgets & rate limits

Give each `PROXY_API_KEYS` entry an upstream-usage ceiling. These
env vars are **global defaults**; set per-key overrides in `auth.json` with `hr limit set`.
`0` = unlimited (the default — no enforcement). Live usage shows in `/v1/status` and `hr status`.

| Variable | Default | Purpose |
|---|---|---|
| `PROXY_LIMIT_RPM` | `0` | Requests/minute per key (rolling 60s window) |
| `PROXY_LIMIT_REQ_DAY` | `0` | Requests per UTC day, per key |
| `PROXY_LIMIT_TOKENS_DAY` | `0` | Tokens per UTC day, per key |
| `PROXY_LIMIT_COST_DAY` | `0` | Estimated USD cost per UTC day, per key (see Cost awareness below) |

```bash
hr limit set sk-team-1 --rpm 60 --req-day 500 --tokens-day 100000 --cost-day 5   # per-key, written to auth.json
hr limit list                                                                    # show all
hr restart                                                                       # apply
```

Exceeding a limit returns `429` with a clear message and a `Retry-After` header. Per-key limits
in `auth.json` look like:

```json
{ "proxy_keys": { "sk-team-1": { "rpm": 60, "req_per_day": 500, "tokens_per_day": 100000, "cost_per_day": 5 } } }
```

> **Security boundary:** every proxy key can also call the dashboard's configuration-write
> endpoints. Limits and provider scopes restrict routing usage, but they do not create a
> read-only or non-admin role. Give keys only to trusted users/services and put an external
> auth layer in front if you need tenant isolation.

### Cost / spend awareness

The router estimates **spend** from a built-in, manually maintained price table (USD per 1M
tokens, input/output). Providers and subscription plans marked as zero-cost in that table show
`$0`. Estimated cost appears per
provider and per key in `/v1/usage`, `/v1/status`, `hr status`, the VS Code dashboard, and
`/metrics` (`hermes_router_cost_usd_total`). USD is always the canonical figure.

| Variable | Default | Purpose |
|---|---|---|
| `COST_CURRENCY` | `USD` | A second currency to *also* display (e.g. `INR`) — requires `COST_FX_RATE` |
| `COST_FX_RATE` | `0` | USD→`COST_CURRENCY` multiplier (e.g. `83`); `0` shows USD only |
| `MODEL_PRICES_FILE` | *(unset)* | JSON file of price overrides — `{"model-substr": [input, output]}` (USD per 1M tokens) — merged over the built-in table |

These are **best-effort operational estimates, not provider invoices**. Prices, promotions,
tokenizers, and subscription terms drift; unknown models may show `$0`. Correct known prices
with `MODEL_PRICES_FILE`.

### Local model (Ollama / LM Studio / llama.cpp)

Set either of the first two to enable a `local` provider pointing at a model on your own
machine. It's keyless (cloud providers remain the fallback). See
[providers.md → Local models](providers.md).

| Variable | Default | Purpose |
|---|---|---|
| `LOCAL_BASE_URL` | `http://localhost:11434/v1` | Your local server's OpenAI-compatible endpoint (LM Studio: `:1234/v1`) |
| `LOCAL_MODEL` | `llama3.1` | Local model id (comma-separate for multi-model failover) |
| `LOCAL_API_KEY` | `local` | Only if your local server actually requires a key |
| `LOCAL_EMBED_MODEL` | *(unset)* | Optional — also serve `/v1/embeddings` from the local server |

> Send model `hermes-router:fast` (or header `X-Hermes-Profile: fast`) to prefer the local model
> for short/casual turns, with cloud fallback for heavier requests.

### Per-provider model

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Model override (set via `hr model set`) |
| `CODEX_MODEL` | `gpt-5.5` | Codex (ChatGPT subscription) model — see [providers.md](providers.md) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model override (set via `hr model set`) |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Model override (set via `hr model set`) |
| `<PROVIDER>_MODEL` | *(varies)* | Same pattern applies to all providers |

### Per-provider embeddings

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | Embedding model (empty disables this provider for `/v1/embeddings`) |
| `<PROVIDER>_EMBED_MODEL` | *(gemini/mistral/cohere set)* | Same pattern for embeddings; set empty to disable |

### Per-provider capability overrides

The router auto-probes each provider at startup, but you can force the result:

| Variable | Default | Purpose |
|---|---|---|
| `<PROVIDER>_SUPPORTS_TOOLS` | *(auto-probed)* | Force tool-capability on/off (`1`/`0`) |
| `<PROVIDER>_REASONING` | *(auto-probed)* | Force reasoning-model on/off (`1`/`0`) |
| `<PROVIDER>_SKIP_TOKENS_OVER` | *(per provider)* | Skip this provider when an estimated request exceeds this many tokens (`0` = never) |
| `<PROVIDER>_MAX_OUTPUT_TOKENS` | *(per provider)* | Clamp `max_tokens` down to this provider's output ceiling (`0` = no clamp) |

## Model overrides

Each provider has a default model that works out of the box. Switch models without editing
files:

```bash
hr model list                              # see all providers and their active model
hr model set anthropic claude-sonnet-4-6   # upgrade Anthropic to Sonnet
hr model set openai gpt-4o                 # use full GPT-4o instead of mini
hr model set gemini gemini-2.5-pro         # switch Gemini to Pro
hr model reset anthropic                   # revert back to the default
hr restart                                 # apply changes
```

Overrides are stored as plain variables in `.env` (e.g. `ANTHROPIC_MODEL=claude-sonnet-4-6`)
and active overrides are highlighted in `hr model list`.

### Multiple models per provider

A provider can use **several models** — just give `<PROVIDER>_MODEL` a comma-separated list:

```bash
hr model set gemini gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.0-flash
hr restart
```

Some providers enforce model-specific limits, so each configured model can be another failover
candidate. When the first model returns `429`, the router **fails over to the next model on
the same key** before cascading to the next provider. This does not bypass limits shared at the
account, project, organization, token, or daily level. Each model is **also a first-class
routing candidate**, scored on its own rating and capability — so the router can pick the right
model in the list for each request (e.g. a stronger model for a hard or tool-using turn), not just
fall over to it. Within equal cost/capability buckets, the router prefers known stronger model
families, then falls back to your listed order.

> **Mixing model classes is fine.** Tool-calling and reasoning are detected **per model** at
> startup, so you can safely list models of different classes (e.g.
> `gemini-2.5-flash-lite,gemini-2.5-pro`) — each is routed and gated on its own capability. Force a
> result per model with `<PROVIDER>_<MODEL>_SUPPORTS_TOOLS` / `_REASONING` (model id upper-cased,
> non-alphanumerics → `_`, e.g. `GEMINI_GEMINI_2_5_PRO_SUPPORTS_TOOLS=1`); the provider-wide
> `<PROVIDER>_SUPPORTS_TOOLS` / `_REASONING` still applies as the default for all its models.

### Auto model discovery

Enable `AUTO_DISCOVER_MODELS=1` (or `hr features enable model_discovery`) to have the
router query configured providers' OpenAI-compatible `/models` endpoints at startup. It
keeps the configured models that still exist, appends the best discovered models up to
`AUTO_DISCOVER_MODEL_LIMIT`, and updates the in-memory routing pool for that run.

This is opt-in because some gateways expose paid or very large catalogs. Known mixed
free/paid gateways are filtered to free model ids where possible, and very large/special
providers such as Hugging Face are skipped unless you opt in per provider with
`HUGGINGFACE_AUTO_DISCOVER_MODELS=1`.

### Per-provider exclude list

To permanently block specific model IDs for a provider — even when listed in
`{PROVIDER}_MODEL` or re-added by auto-discovery — set `{PROVIDER}_EXCLUDE_MODELS`:

```bash
OPENROUTER_EXCLUDE_MODELS=some/unwanted-model:free
MISTRAL_EXCLUDE_MODELS=mistral-tiny
SAMBANOVA_EXCLUDE_MODELS=gemma-4-31B-it
```

Excluded models are matched case-insensitively (exact ID only, no globs). The
filter applies both to your configured model list and to any extras appended by
auto-discovery. If every model for a provider is excluded, the provider is shown
in status but skipped for routing until at least one usable model is configured,
and a warning is logged at startup.

## Key rotation mode

When a provider holds several keys (or several accounts), `ROTATION_MODE` decides how the
router picks among them. Set it in the dashboard's Configuration panel or with:

```bash
hr mode                # show the current mode
hr mode round-robin    # default — spread requests evenly across all keys
hr mode sequential     # drain one key fully before moving to the next
hr restart             # apply the change
```

- **`round-robin`** (default) — every request goes to the next key in turn, so all keys
  share the load and deplete together. Best for spreading latency and load.
- **`sequential`** — one key is used until it hits its rate limit, then the router moves to
  the next, and so on. Later keys stay **untouched in reserve** — useful when you want to
  ration accounts after a quota reset instead of burning them all at once. Keys are drained
  in the order they appear in `auth.json`.

Either way, failover, per-key cooldowns, and the circuit breaker keep working — the mode
only changes *which ready key is preferred* next. The active mode shows in `hr status` and
at `/v1/status`.

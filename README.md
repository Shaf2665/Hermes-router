# hermes-router

![Hermes Router](hermes-router-banner.png)

[![Docker Hub](https://img.shields.io/docker/v/shafiq735/hermes-router?label=Docker%20Hub&logo=docker&sort=semver)](https://hub.docker.com/r/shafiq735/hermes-router)
[![VS Code Marketplace](https://img.shields.io/visual-studio-marketplace/v/MohammedShafiq.hermes-router?label=VS%20Code&logo=visualstudiocode)](https://marketplace.visualstudio.com/items?itemName=MohammedShafiq.hermes-router)

**Stretch free AI quotas across providers.** hermes-router sits between your app and a pool
of AI providers (Gemini, OpenRouter, Groq, and more). When one provider hits its rate limit,
it automatically tries the next. Requests can still fail when every configured option is
unavailable or exhausted.

It implements the commonly used OpenAI-compatible chat, embeddings, and models endpoints,
plus Anthropic's Messages endpoint. Compatible clients can usually connect by changing their
base URL and API key; unsupported OpenAI/Anthropic endpoints are listed in
**[Usage](documentation/usage.md)**.

```
  Your app ──────► hermes-router ──► Gemini → OpenRouter → Groq → … (tries each until one works)
 (OpenAI SDK or    localhost:8319
  Anthropic SDK)
```

**Highlights:** OpenAI-compatible chat/embeddings/models + Anthropic Messages · automatic key rotation &
failover · smart routing (sends each request to the cheapest model that can handle it) ·
**local models** (Ollama / LM Studio) with cloud fallback · tool calling · embeddings ·
response caching (incl. optional **semantic** cache) · **per-key budgets & rate limits** ·
**built-in web dashboard** (just open `http://localhost:8319/`) · **multi-instance
manager** (monitor existing routers or launch Docker-backed routers) · **usage analytics**
(`/v1/usage`) with **cost/spend tracking** · circuit breaker for unhealthy providers ·
Prometheus `/metrics` · **runs as a reboot-surviving service** (`hr service`) · toggle
optional features with `hr features` · one structured `auth.json` for all your keys.

## Documentation

The docs read in order, from zero experience to a running, monitored agent:

**Start here** (assume no experience):

- 🚀 **[Getting started](documentation/getting-started.md)** — what this is, key terms, your first message
- 📖 **[Concepts](documentation/concepts.md)** — plain-language glossary (LLM, token, agent, RAG…)
- 🧭 **[Routing Features](documentation/routing.md)** — plain-language guide to how requests get routed (chat, tools, vision, embeddings)

**Set it up:**

- **[Deployment](documentation/deployment.md)** — Windows/macOS/Linux, Docker, Hugging Face Spaces, **surviving reboots**
- **[Providers](documentation/providers.md)** — free & paid providers, sign-up links, capabilities
- **[Free model rankings](documentation/free-model-rankings.md)** — compare the default free models by quality and best use
- **[Configuration](documentation/configuration.md)** — `auth.json`, main `.env` settings and provider patterns, **core features vs. add-ons** (`hr features`)

**Build with it:**

- **[Usage](documentation/usage.md)** — OpenAI SDK, Anthropic SDK, tool use, embeddings
- 🤖 **[Build your first AI agent](documentation/build-an-agent.md)** — chatbot → memory → tools, copy-paste

**Operate & extend:**

- **[Monitoring](documentation/monitoring.md)** — **web dashboard** (`/dashboard`), **Instances** manager, `hr status`, Prometheus `/metrics`, `/v1/status` (tokens, spend)
- **[VS Code Extension](documentation/vscode-extension.md)** — monitor & manage the router, and use it as a model in Copilot Chat

---

## Architecture

A single Python file (`router.py`) running a small Flask/Waitress server. One request
flows through it like this:

```
  ┌──────────┐   OpenAI-format request    ┌─────────────────────────────────────┐
  │ Your app │ ─────────────────────────► │            hermes-router            │
  └──────────┘   Bearer PROXY_API_KEYS    │                                     │
       ▲                                   │  1. Auth check (PROXY_API_KEYS)     │
       │                                   │  2. Cache lookup (exact match)      │
       │         OpenAI-format response    │  3. Rate the request (1–5)          │
       └────────────────────────────────► │  4. Order providers by fit + health │
                                           │  5. Try providers, rotate keys      │
                                           └───────────────┬─────────────────────┘
                                                           │ first one that succeeds
                                           ┌───────────────▼─────────────────────┐
                                           │ Gemini · OpenRouter · Groq · Mistral │
                                           │ Cohere · NVIDIA · Codex · Kimi · more │
                                           └──────────────────────────────────────┘
```

**The moving parts:**

- **Credential pool** — every provider can hold many keys (from `auth.json`, then `.env`).
  Key selection is synchronized for concurrent requests and rotates round-robin; a key that
  gets rate-limited is put on a short cooldown and skipped until it recovers. Each key's usage
  count is visible in `/v1/status` and the web dashboard, so you can watch load spread across
  keys as you add them.
- **Smart routing** — each request is scored 1–5 for difficulty (by length and content, no
  extra API call), and each model is scored 1–5 for capability. The router picks the
  *cheapest* model that can still handle the request, and rotates among equally-good ones.
  Tool-capable models are preferred; when capability is unknown for every candidate, the
  router tries the pool instead of rejecting the request before an upstream attempt.
- **Failover** — if a provider errors or times out, the router cascades to the next one
  automatically while another configured candidate remains available.
- **Circuit breaker** — a provider that keeps failing is pulled out of rotation for a
  cooldown, then re-probed. Healthy providers are always preferred.
- **Response cache** — identical requests can be served from an in-memory cache (TTL-based),
  saving free-tier quota.

Everything is configured by environment variables and `auth.json`
(see **[Configuration](documentation/configuration.md)**). Nothing is hidden or installed
system-wide — `install.sh` only symlinks the `hr` command onto your PATH.

---

## Setup

**Requirements:** Python 3.10+ and at least one configured cloud provider key or local model
(see **[Providers](documentation/providers.md)**).

> **Platform note:** the router runs on **Linux, macOS, and Windows**. The one-liner and
> `hr` CLI below are for Linux/macOS (and WSL2). On **Windows** use Docker, WSL2, or run
> `python router.py` directly — see **[Deployment](documentation/deployment.md)**.

### One-liner install

```bash
curl -fsSL https://raw.githubusercontent.com/Shaf2665/Hermes-router/main/get.sh | bash
```

This clones the repo to `~/.local/share/hermes-router`, creates a venv, installs
dependencies, and puts `hr` on your PATH — all in one step. Then run the interactive setup
wizard, which walks you through adding your first API key and starting the router:

```bash
hr setup
```

### Manual install (if you already cloned)

```bash
git clone https://github.com/Shaf2665/Hermes-router.git
cd Hermes-router
./install.sh     # creates venv, installs deps, symlinks hr
hr setup         # interactive wizard: add a key + start the router
```

Check it's running:

```bash
curl http://localhost:8319/health
```

Or open **`http://localhost:8319/`** in a browser for the live monitoring dashboard —
provider health, request log, cache stats, per-key usage, and an **Instances** tab for
tracking other Hermes routers or launching Docker-backed ones (it'll ask for your proxy key).

### Quick start

Point an OpenAI chat-completions client at `http://localhost:8319/v1`, model `hermes-router`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8319/v1", api_key="YOUR_ROUTER_KEY")
resp = client.chat.completions.create(
    model="hermes-router",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

The Anthropic SDK works the same way (point `base_url` at `http://localhost:8319`), and the
router also serves embeddings and tool calls — see **[Usage](documentation/usage.md)** for
all of it.

---

## Commands

The `./install.sh` step puts `hr` (and the full name `hermes-router`) on your PATH.

| Command | What it does |
|---|---|
| `hr setup` | Interactive first-run wizard — add a key, start the router, verify it works |
| `hr auth add <provider>` | Add one or more API keys for a provider (prompts you, input hidden) |
| `hr auth import-codex` | Import a ChatGPT-subscription login from the Codex CLI (OAuth) |
| `hr auth list` | Show every provider and how many keys it has |
| `hr model list` | Show every provider and its active model (default or overridden) |
| `hr model set <provider> <model>` | Override the model used for a specific provider |
| `hr model reset <provider>` | Revert a provider back to its default model |
| `hr mode` | Show how keys are rotated within a provider |
| `hr mode <round-robin\|sequential>` | Set the key rotation mode (see [Configuration](documentation/configuration.md)) |
| `hr limit set <key> [opts]` | Set a proxy key's rate/budget limits (`--rpm`/`--req-day`/`--tokens-day`/`--cost-day`) |
| `hr features list` | Show core features + optional add-ons, with on/off state |
| `hr features enable\|disable <name>` | Turn an add-on on/off (writes `.env`; see [Configuration](documentation/configuration.md)) |
| `hr start` | Run the router (same as `python router.py`) |
| `hr service install` | Run as a service that **survives reboots** (systemd; `status`/`uninstall` too) |
| `hr status` | Live dashboard — per-provider health, latency, cache stats |
| `hr restart` | Restart the router so key/config changes take effect |
| `hr doctor` | Diagnose installation issues (Python, venv, keys, PATH, router health) |
| `hr update` | Update to the latest version (safe; auto-rolls-back on failure) |
| `hr version` | Show the installed version |
| `hr help` | Show all commands |

Settings live in `.env` — see **[Configuration](documentation/configuration.md)** for the
full reference, and **[Providers](documentation/providers.md)** for valid provider names.

---

## Troubleshooting

**`All providers exhausted` / requests fail** — every provider is rate-limited or has no
keys. Run `hr auth list` to confirm keys are loaded, and `hr status` to see which are
cooling down. Add more keys: `hr auth add <provider>`.

**`401 Unauthorized`** — your app's API key isn't in `PROXY_API_KEYS`. On first boot the
router generates a random key, saves it to `.env`, and logs it once. Use that value, or set
your own in `.env` and run `hr restart`.

**A provider never gets used** — check `hr status`. If its circuit breaker is open it was
unhealthy and is cooling off; it'll be re-probed automatically. If it shows `no keys`, add
some with `hr auth add`.

**Keys not picked up after adding** — you must `hr restart` for new keys to load.

**Port already in use** — something else is on `8319`. Set `PORT=8320` in `.env` (and point
your app at the new port), then `hr restart`.

**Empty replies on short requests** — some reasoning models spend the whole budget
thinking; the router reserves extra headroom automatically, but you can raise
`REASONING_TOKEN_RESERVE`. See [Configuration](documentation/configuration.md).

**Check it's alive** — `curl http://localhost:8319/health` should return `{"status":"ok",...}`.
For detail, `hr status` or watch the logs (`router.log`, or `journalctl -u hermes-router` if
you run it as a systemd service).

---

## License

MIT — see [LICENSE](LICENSE).

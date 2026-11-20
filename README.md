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

The docs read in order, from zero experience to a running, monitored router:

**Start here** (assume no experience):

- 🚀 **[Getting started](documentation/getting-started.md)** — what this is, key terms, your first message
- 📖 **[Concepts](documentation/concepts.md)** — plain-language glossary (LLM, token, agent, RAG…)
- 🧭 **[Routing Features](documentation/routing.md)** — plain-language guide to how requests get routed (chat, tools, vision, embeddings)

**Set it up:**

- **[Local setup](documentation/setup.md)** — Windows, macOS and Linux, with or without Docker
- **[Server setup](documentation/deployment.md)** — Linux/Windows Server, remote access, backups and automatic startup
- **[Providers](documentation/providers.md)** — free & paid providers, sign-up links, capabilities
- **[Free model rankings](documentation/free-model-rankings.md)** — compare the default free models by quality and best use
- **[Configuration](documentation/configuration.md)** — `auth.json`, main `.env` settings and provider patterns, **core features vs. add-ons** (`hr features`)

**Build with it:**

- **[Usage](documentation/usage.md)** — OpenAI SDK, Anthropic SDK, tool use, embeddings
- 🤖 **[Build your first AI agent](documentation/build-an-agent.md)** — chatbot → memory → tools, copy-paste
- 🏛️ **[Hall of Wisdom integration](documentation/hall-of-wisdom.md)** — use Hermes as Hall's multi-provider inference gateway

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

Start with [the beginner guide](documentation/getting-started.md), then choose one path:

- [Your computer](documentation/setup.md): Windows, macOS or Linux, with Python or Docker.
- [A server](documentation/deployment.md): Linux/Windows Server, remote access and automatic startup.
- [VS Code](documentation/vscode-extension.md): connect Copilot Chat after your first dashboard test works.

The guides use the published `custom-20261104` Docker image or the
`codex/hermes-upgrade-design` source branch. They explain separate administrator
and application keys, persistent storage, live provider changes and backups.
The 0.9.8 context-routing feature remains a source preview requiring matching
router and extension updates; the dated Docker image does not include it.

Your dashboard runs at `http://localhost:8319` for a local installation.
Sign in with the generated administrator key, add a provider in **Providers**,
then create an application key and send a test message in **Connect**.
Use that application key in your app; keep the administrator key private.

Hermes is free software. Upstream AI accounts, quotas and charges are your own.
For SDK examples, see [Use the API](documentation/usage.md).

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

**Keys not picked up after adding** — dashboard saves apply immediately. Keys added through `hr auth add` or direct file edits still need `hr restart`.

**Port already in use** — something else is on `8319`. Set `PORT=8320` in `.env` (and point
your app at the new port), then `hr restart`.

**Empty replies on short requests** — some reasoning models spend the whole budget
thinking; the router reserves extra headroom automatically, but you can raise
`REASONING_TOKEN_RESERVE`. See [Configuration](documentation/configuration.md).

**Check it's alive** — `curl http://localhost:8319/health` should return `{"status":"ok",...}`.
For detail, `hr status` or watch the logs (`router.log`, or `journalctl -u hermes-router` if
you run it as a systemd service).

---

## Star History

<a href="https://www.star-history.com/?repos=shaf2665%2Fhermes-router&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=shaf2665/hermes-router&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=shaf2665/hermes-router&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=shaf2665/hermes-router&type=date&legend=top-left" />
 </picture>
</a>

## License

MIT — see [LICENSE](LICENSE).

### Provider presets

The Providers dashboard includes 65 API-key presets: choose a provider, open its key link, paste your key and select multiple models. Base URLs are already filled in. See the [provider directory and setup steps](documentation/providers.md#add-a-provider-from-the-dashboard). Free offers, trial credits and paid subscriptions are labeled separately; verify current prices with each provider.

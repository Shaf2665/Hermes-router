# Getting started (no experience needed)

New to AI and not sure what any of this means? Start here. This page explains what
hermes-router is, the handful of words you'll see everywhere, and how to send your very
first message — step by step.

## What is hermes-router, in one sentence?

It's a **free middleman** between your program and AI models. Your program asks it a
question; it finds a configured AI provider and gets you an answer, switching to another
provider automatically if one is busy or rate-limited.

**An analogy:** imagine a phone operator with a stack of calling cards from different
networks. You ask to make a call; the operator tries one card, and if it's out of minutes,
tries the next. If every card is unavailable, the call still fails. hermes-router is that operator, and
the "cards" are free API keys from providers like Google Gemini, Groq, and others.

## Why would I want it?

- **It can use free access tiers.** Provider availability, quotas, and terms still apply.
- **It reduces single-provider interruptions.** When one provider hits its limit, it tries
  another configured provider.
- **It is client-compatible for common endpoints.** OpenAI chat/embeddings/models and
  Anthropic Messages clients usually need only a base URL and key change.

## Words you'll keep seeing

You don't need to memorize these — skim them, and come back when one shows up. There's a
fuller list in **[concepts.md](concepts.md)**.

- **LLM** (Large Language Model) — the actual AI "brain" that reads text and writes a reply
  (e.g. GPT, Gemini, Claude, Llama).
- **API key** — a secret password that lets your program use a provider. You get these free
  from the providers (see **[providers.md](providers.md)**).
- **Provider** — a company that hosts LLMs you can call over the internet (Gemini, Groq…).
- **Token** — roughly ¾ of a word. AI usage and limits are measured in tokens.
- **Prompt** — the text you send to the AI.

## Step 1 — Install it

```bash
curl -fsSL https://raw.githubusercontent.com/Shaf2665/Hermes-router/main/get.sh | bash
```

This downloads hermes-router and adds an `hr` command to your terminal.

## Step 2 — Add your first free key

Run the friendly setup wizard:

```bash
hr setup
```

It will ask which provider you have a key for and walk you through it. Don't have one yet?
**Gemini** is a common place to start — get a key at
[aistudio.google.com](https://aistudio.google.com), then paste it when asked. (More options
in **[providers.md](providers.md)**.)

## Step 3 — Check it's running

```bash
curl http://localhost:8319/health
```

You should see `{"status":"ok",...}`. That means the router is up and listening.

> **Want it to stay up after a reboot?** On a server, run `hr service install` so the router
> starts on boot and restarts itself if it crashes (`hr setup` also offers this). See
> **[deployment.md](deployment.md)** → "Keep it running".

## Step 4 — Send your first message

The router supports the OpenAI SDK's chat completions interface. Install the library and
run this:

```bash
pip install openai
```

```python
from openai import OpenAI

# This is the generated PROXY_API_KEYS value from .env, not a provider key.
client = OpenAI(base_url="http://localhost:8319/v1", api_key="YOUR_ROUTER_KEY")

reply = client.chat.completions.create(
    model="hermes-router",
    messages=[{"role": "user", "content": "Explain what an AI agent is, simply."}],
)
print(reply.choices[0].message.content)
```

Run it and you have made your first call through hermes-router. The upstream cost is zero
only when the selected provider/model is currently free for your account.

## Where to go next

- **Want to build something that *does* things, not just chat?** → **[build-an-agent.md](build-an-agent.md)**
  walks you from a chatbot to a real AI agent, step by step.
- **Confused by a term?** → **[concepts.md](concepts.md)** is a plain-language glossary.
- **Curious how it picks a provider for chat, tools, or images?** → **[routing.md](routing.md)**.
- **Want more providers / more reliability?** → **[providers.md](providers.md)**.
- **Want to change settings?** → **[configuration.md](configuration.md)**.

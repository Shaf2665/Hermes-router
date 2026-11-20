---
title: "Usage"
description: "Call the router from the OpenAI SDK or Anthropic SDK, with tool use, embeddings and reasoning models."
---

hermes-router implements these client-facing endpoints:

- OpenAI-compatible `POST /v1/chat/completions`, `POST /v1/embeddings`, and `GET /v1/models`
- Anthropic-compatible `POST /v1/messages`

Clients using those endpoints normally need only a base URL and API-key change. Other API
surfaces, including image generation, audio, assistants, batches, files, and the OpenAI
Responses API, are not exposed as client-facing router endpoints.

First complete [setup](/setup/) and get a test reply in **Connect**. Create an
application key there for your script. That is your `api_key`; do not use a
provider key or the administrator key. Existing `PROXY_API_KEYS` also work.

## OpenAI SDK

This is optional if you only want to use the VS Code extension. To try a Python
script, create a folder named `hello-hermes` and open a terminal in it. With Python
installed, run the commands for your operating system.

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install openai
```

**Mac/Linux Terminal:**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install openai
```

This keeps the example's libraries in that folder. Save the following as
`hello_hermes.py` in the same folder:

```python
from openai import OpenAI
from getpass import getpass

client = OpenAI(base_url="http://localhost:8319/v1", api_key=getpass("Hermes application key: "))
resp = client.chat.completions.create(
    model="hermes-router",
    messages=[{"role": "user", "content": "Hello!"}],
    max_tokens=64,
)
print(resp.choices[0].message.content)
```

Run `.\.venv\Scripts\python.exe hello_hermes.py` on Windows or
`.venv/bin/python hello_hermes.py` on Mac/Linux.
Paste the application key when prompted (it is hidden). You should see a reply.
Keep the router running. For a remote router, use its HTTPS address or your open
SSH tunnel's address. See [Server setup](/deployment/).

Streaming (`stream=True`) and function calling (`tools=[...]`) both work with
eligible models.

> **Tip — add model-level failover:** give a provider several models with a comma-separated
> `<PROVIDER>_MODEL` (e.g. `GEMINI_MODEL=gemini-2.5-flash-lite,gemini-2.5-flash`). Since
> some providers enforce model-specific limits, the router can fail over across a provider's
> models before moving on. Quota is not guaranteed to multiply: providers may enforce shared
> project, organization, token, or daily limits. See
> [Configuration](/configuration/#multiple-models-per-provider).

## Anthropic SDK

Already built on the Anthropic Messages API? Point its `base_url` at hermes-router. The
router accepts `/v1/messages` format (and the `x-api-key`
header), translates it, and routes across **all** your free providers:

```python
import anthropic

client = anthropic.Anthropic(api_key="YOUR_ROUTER_KEY", base_url="http://localhost:8319")
msg = client.messages.create(
    model="claude-3-5-sonnet-20241022",   # model name is ignored — the router picks
    max_tokens=100,
    messages=[{"role": "user", "content": "Hello!"}],
)
print(msg.content[0].text)
```

Streaming (`client.messages.stream(...)`) works too.

> The `model` you pass is **ignored** — Hermes selects among the eligible models
> in your configured pool, so an Anthropic-SDK app gets the same multi-provider failover.
> (Use the `openai`/`anthropic` paid providers if you specifically want those models.)

### Tool use

Anthropic `tools`, `tool_use`, and `tool_result` are translated to/from OpenAI function
calling in both streaming and non-streaming mode — full round-trips work:

```python
tools = [{
    "name": "get_weather",
    "description": "Get the current weather for a city",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
}]
msg = client.messages.create(
    model="claude-3-5-sonnet-20241022", max_tokens=300, tools=tools,
    messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
)
# msg.stop_reason == "tool_use", with a tool_use block ready to run
```

When a request carries tools, the router prefers models confirmed to support function calling.
If none can be confirmed, it tries the configured pool instead of rejecting the request before
an upstream attempt. Override detection per provider with
`<PROVIDER>_SUPPORTS_TOOLS=1` / `=0` (see [configuration.md](/configuration/)).

## Embeddings

The router also implements the OpenAI **embeddings** API, backed by configured embedding
providers (Gemini, Mistral, Cohere, OpenAI, or a configured local embedding model):

```python
resp = client.embeddings.create(model="hermes-router", input="hello world")
print(len(resp.data[0].embedding))   # e.g. 3072 from Gemini
```

Unlike chat, embeddings use a **stable provider** (not round-robin): vectors from
different providers have different dimensions and can't be mixed in one store, so the
router keeps hitting the same provider and only fails over if it goes down. For a strict
single-dimension guarantee, disable the others' embed models (e.g. `MISTRAL_EMBED_MODEL=`
and `COHERE_EMBED_MODEL=` empty in `.env`).

## Reasoning models

Some models (e.g. gpt-oss, Nemotron, and GLM reasoning models) spend output tokens on
internal reasoning before answering. The router detects these at startup and reserves extra
output budget to reduce empty replies caused by a small `max_tokens`. Tune with
`REASONING_TOKEN_RESERVE` (see [configuration.md](/configuration/)).

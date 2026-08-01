---
title: "Routing Features"
description: "Plain-language guide to how hermes-router decides where each request goes — chat, tools, vision, and embeddings."
---

hermes-router doesn't just forward your request to the first provider it finds — it looks at
*what kind* of request it is and picks a provider that can actually handle it, then falls back
automatically if that one fails. This page explains each routing feature in plain language. For
the technical version (scoring formulas, code paths), see **[Architecture](/architecture/)**.

**The short version:** every request gets scored for difficulty, every model is scored for
capability, and the router sends it to the cheapest model that can still do the job — skipping
anything that can't, and trying the next option automatically if one fails.

## Chat completion routing

This is the core of the router, used by every request.

- Your message gets a **difficulty score from 1 (hardest) to 5 (easiest)** just from reading it —
  no extra AI call. Words like "implement," "design," or "debug" push it toward "hard"; something
  like "what year was X" or "yes or no" pushes it toward "easy."
- Every configured model has a **capability rating from 1 (strongest) to 5 (weakest)**, based on
  its name (e.g. `gemini-2.5-pro` rates higher than `gemini-2.5-flash-lite`).
- The router prefers the **lowest configured cost tier whose rating is sufficient** for the
  heuristic difficulty score. These ratings are routing hints, not a guarantee of answer quality.
- If the chosen model is rate-limited, down, or errors, the router **automatically tries the next
  best one**. If every candidate fails, the app receives a `503`.

This also works *within* a single provider: if you list several models for one provider (e.g.
`GEMINI_MODEL=gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.5-pro`), the router treats each one
as its own candidate — easy requests land on `flash-lite`, hard ones climb to `gemini-2.5-pro` —
instead of only using the extra models as backups.

## Tool-calling routing

**Tool calling** (or "function calling") is how you let the model *do* something — like look up
the weather — instead of just answering in text. See **[Concepts](/concepts/)** if that's new to you.

Not every model can do this. When your request includes `tools`, the router first considers
models known to support function calling, skipping ones known not to support it. This is detected per model at
startup (see [Configuration → Per-provider capability overrides](/configuration/#per-provider-capability-overrides)
to override a result manually with `<PROVIDER>_SUPPORTS_TOOLS`).

**Safety net:** if the router can't confirm *any* candidate supports tools (e.g. every provider is
new/unprobed), it doesn't hard-fail — it falls back to trying all of them rather than refusing the
request outright.

## Vision routing

When your request includes an image, the router **only considers models known to accept image
input**, for the same reason as tool-calling: sending an image to a text-only model just wastes a
round-trip on a guaranteed rejection.

This works whether your app talks to the router in **OpenAI format** (`image_url` content blocks)
or **Anthropic format** (`image` content blocks via `/v1/messages`) — both are translated correctly
so the image actually reaches the model, regardless of which SDK you're using.

Same safety net as tool-calling: if no known vision-capable candidate exists among your configured
providers, the router falls back to trying all of them instead of refusing the request.

## Embeddings routing

**Embeddings** turn text into a list of numbers representing its *meaning*, used for things like
"find the most similar document." See **[Concepts](/concepts/)** for the plain-language version.

This is a separate, simpler routing path — only providers configured with an embedding model
(currently Gemini, Mistral, Cohere, and OpenAI) are candidates. `POST /v1/embeddings` uses the
same failover behavior as chat completions: if one embedding provider fails, the next is tried
automatically.

## What isn't routed (yet)

To set expectations clearly: hermes-router does **not** currently route requests for **image
generation** (there's no DALL-E-style "create an image" endpoint) or **audio/speech**. It only
routes text and vision *input* — reading images you send it, not generating new ones.

## Want the full technical picture?

This page is the plain-language tour. For the exact scoring formula, the request pipeline
diagram, and how each of these interacts with failover, the circuit breaker, and capability
probing under the hood, see **[Architecture — How it works](/architecture/)**.

---

**Next:** [Deployment](/deployment/) — run the router on your OS (Windows, macOS, Linux, Docker, or Hugging Face Spaces).

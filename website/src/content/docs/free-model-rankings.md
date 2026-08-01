---
title: "Free Model Rankings"
description: "Rank the free models built into Hermes Router by quality, speed, use case, and provider availability."
---

This page ranks the hosted models that Hermes Router uses by default on a free
tier, free trial endpoint, or included monthly credit. It does **not** include
OpenAI, Anthropic, Codex, Kimi, or OpenCode Go because those require paid API
usage or a subscription. Local models are separate because their quality and
speed depend on the model and hardware you choose.

**Last verified: July 31, 2026.**

## Quick picks

| Need | Start with | Provider |
|---|---|---|
| Best overall quality | `deepseek-v4-flash-free` | OpenCode Zen |
| Strong agent reasoning | `nemotron-3-ultra-free` | OpenCode Zen |
| Multimodal work | `mimo-v2.5-free` | OpenCode Zen |
| Fastest reasoning endpoint | `openai/gpt-oss-120b` | Cerebras |
| Stable general-purpose model | `mistral-medium-3-5` | Mistral |
| Coding on a smaller model | `north-mini-code-free` | OpenCode Zen |
| Large multimodal context | `gemini-2.5-flash-lite` | Gemini |

## Quality ranking

The order is based primarily on the
[Artificial Analysis Intelligence Index](https://artificialanalysis.ai/methodology/intelligence-benchmarking),
which combines reasoning, knowledge, coding, tool-use, and long-context evaluations.
The score measures model quality, **not endpoint speed or free quota size**. A higher
score is better. Scores can change when the benchmark suite is updated.

| Rank | Model | Hermes providers | AA score | Best for | Free-tier note |
|---:|---|---|---:|---|---|
| 1 | `deepseek-v4-flash` | NVIDIA NIM, OpenCode Zen | [40](https://artificialanalysis.ai/models/deepseek-v4-flash) | Coding, reasoning, long agent runs | NVIDIA trial endpoint; OpenCode promotion |
| 2 | `nemotron-3-ultra` | OpenCode Zen | [38](https://artificialanalysis.ai/models/nvidia-nemotron-3-ultra-550b-a55b/) | Agents, orchestration, tool use | Temporary OpenCode promotion |
| 3 | `mimo-v2.5` | OpenCode Zen | [37](https://artificialanalysis.ai/models/mimo-v2-5-0424/) | Vision, long context, reasoning | Temporary OpenCode promotion |
| 4 | `DeepSeek-V3.2` | SambaNova | [32](https://artificialanalysis.ai/models/deepseek-v3-2-reasoning) | Reasoning and concise answers | Developer free tier |
| 5 | `mistral-medium-3-5` | Mistral | [30](https://artificialanalysis.ai/models/mistral-medium-3-5/) | General agents and multimodal work | Free mode has evaluation limits |
| 6 | `nemotron-3-super-120b-a12b:free` | OpenRouter, Naga AI | [25](https://artificialanalysis.ai/models/nvidia-nemotron-3-super-120b-a12b/) | Agent workflows with fast decoding | Explicit free model IDs |
| 7 | `openai/gpt-oss-120b` | Cerebras, Groq, Hugging Face | [24](https://artificialanalysis.ai/models/gpt-oss-120b/) | Fast reasoning and tool use | HF is credit-based; Cerebras/Groq are rate-limited |
| 8 | `glm-4.7-flash` | Z.ai | [23](https://artificialanalysis.ai/models/glm-4-7-flash) | Bilingual reasoning, coding, agents | Token price is currently free |
| 9 | `north-mini-code-free` | OpenCode Zen | [20](https://artificialanalysis.ai/models/north-mini-code/) | Focused coding tasks | Temporary OpenCode promotion |
| 10 | `gemini-2.5-flash-lite` | Gemini | [11](https://artificialanalysis.ai/models/gemini-2-5-flash-lite-reasoning/) | Fast multimodal and large-context requests | Google free tier |
| 10 | `gpt-4o` | GitHub Models | [11](https://artificialanalysis.ai/models/gpt-4o/) | Vision and general prototyping | Included quota; not intended for production |
| 12 | `command-a-03-2025` | Cohere | [8](https://artificialanalysis.ai/models/command-a/) | RAG, enterprise text, tool use | Evaluation key: 1,000 calls/month |

The same model can appear through more than one provider. That is useful: adding both
keys gives Hermes Router another quota pool and another endpoint to fail over to without
changing model quality.

## Provider speed and availability

Quality is only half of practical performance. Provider hardware, queueing, rate limits,
and cold starts determine how quickly that model answers.

| Provider | Practical advantage | Important limit |
|---|---|---|
| Cerebras | Extremely high output throughput for `gpt-oss-120b` | Free usage is rate and token limited |
| Groq | Fast `gpt-oss-120b` serving | Free-plan quotas vary by model |
| Gemini | Low latency and a 1M-token multimodal context | Flash-Lite favors speed over hard reasoning |
| OpenCode Zen | Access to several of the strongest free coding models | Free models are temporary and can rotate |
| OpenRouter / Naga | Two independent paths to Nemotron 3 Super | Shared free endpoints may queue under load |
| Hugging Face | One key can reach many inference providers | Free users receive only $0.10 monthly credit |
| GitHub Models | Convenient for prototypes using a GitHub token | Free API use is not intended for production |

Hermes Router handles these differences through key rotation, health scoring, circuit
breaking, and provider failover. For most apps, configure several providers instead of
depending on the number-one model alone.

## Use a ranked model

The built-in defaults already follow this list. To override one provider:

```bash
hr model set groq openai/gpt-oss-120b
hr model set zai glm-4.7-flash
hr model set opencode deepseek-v4-flash-free,nemotron-3-ultra-free,mimo-v2.5-free
hr restart
```

Check the active model and live health before relying on it:

```bash
hr model list
hr status
```

## Sources and caveats

Availability was checked against current provider documentation:
[Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing),
[OpenCode Zen](https://opencode.ai/docs/zen),
[Groq models and limits](https://console.groq.com/docs/models),
[Cerebras rate limits](https://inference-docs.cerebras.ai/support/rate-limits),
[Z.ai pricing](https://docs.z.ai/guides/overview/pricing),
[Cohere trial limits](https://docs.cohere.com/v2/docs/rate-limits),
[GitHub Models free usage](https://docs.github.com/en/billing/concepts/product-billing/github-models),
and [Hugging Face credits](https://huggingface.co/docs/inference-providers/en/pricing).

Free plans, promotions, model aliases, and benchmark results change. Treat this page as a
starting point, then run your own prompts through the models that matter for your app.

---

**Next:** [Configuration](/configuration/) — override defaults and tune routing behavior.

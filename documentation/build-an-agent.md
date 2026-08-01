# Build your first AI agent

A hands-on, copy-paste guide that takes you from a simple chatbot to a real AI agent —
using models routed through hermes-router. The examples need no agent framework and can use
currently available free/evaluation provider tiers; provider quotas and terms still apply.
No prior agent experience is needed.

> New to the words here (LLM, token, tool…)? Skim **[concepts.md](concepts.md)** first.

## Chatbot vs. agent — what's the difference?

- A **chatbot** answers one message at a time.
- An **agent** is given a *goal* and works through **multiple steps** to reach it — thinking,
  using tools, and reacting to what it finds, until the job is done.

Every agent runs the same simple cycle, called the **agent loop**:

```
        ┌─────────────────────────────┐
        │           OBSERVE           │  look at the goal + what's happened so far
        └──────────────┬──────────────┘
                       ▼
        ┌─────────────────────────────┐
        │            THINK            │  decide the next step
        └──────────────┬──────────────┘
                       ▼
        ┌─────────────────────────────┐
        │             ACT             │  reply, or use a tool — then loop again
        └─────────────────────────────┘
```

We'll build up to that in three small steps.

## Before you start

1. hermes-router is installed and running (see **[getting-started.md](getting-started.md)**).
2. Install the OpenAI library. Upstream cost depends on the provider/model your router selects:

```bash
pip install openai
```

Throughout, we use this client (the `api_key` is the router's own password, not a provider
key):

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8319/v1", api_key="YOUR_ROUTER_KEY")
```

---

## Step 1 — A basic chatbot

The smallest useful thing: send a message, print the reply.

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8319/v1", api_key="YOUR_ROUTER_KEY")

reply = client.chat.completions.create(
    model="hermes-router",
    messages=[
        {"role": "system", "content": "You are a friendly assistant. Keep answers short."},
        {"role": "user", "content": "Hi! What can you do?"},
    ],
)
print(reply.choices[0].message.content)
```

The `messages` list is the whole conversation. `system` sets the persona; `user` is you.

---

## Step 2 — Give it memory

The model forgets everything between calls. To hold a conversation, we keep the history in
a list and re-send it every turn. This is **short-term memory**.

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8319/v1", api_key="YOUR_ROUTER_KEY")

messages = [{"role": "system", "content": "You are a friendly assistant. Keep answers short."}]

while True:
    user = input("You: ")
    if user.strip().lower() in ("quit", "exit"):
        break
    messages.append({"role": "user", "content": user})

    reply = client.chat.completions.create(model="hermes-router", messages=messages)
    answer = reply.choices[0].message.content
    print("Bot:", answer)

    messages.append({"role": "assistant", "content": answer})   # remember the reply
```

Now it remembers what you said earlier in the chat. That's already a capable conversational
bot — but it can only *talk*. Let's make it *do* something.

---

## Step 3 — Give it a tool (now it's an agent)

A **tool** is a function the model can choose to call. You describe your functions, the model
decides when to use one, you run it, and you feed the result back — that's the agent loop in
action. Here we give it a (pretend) weather tool.

```python
import json
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8319/v1", api_key="YOUR_ROUTER_KEY")

# 1) The actual function the agent can run.
def get_weather(city):
    return f"It's 22°C and sunny in {city}."   # pretend this calls a real weather API

# 2) Describe it to the model so it knows the tool exists.
tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}]

messages = [{"role": "user", "content": "What's the weather in Paris?"}]

# 3) The agent loop: keep going until the model stops asking for tools.
while True:
    reply = client.chat.completions.create(
        model="hermes-router", messages=messages, tools=tools,
    )
    msg = reply.choices[0].message
    messages.append(msg)                 # OBSERVE: record the model's turn

    if not msg.tool_calls:               # THINK: no tool needed → it's the final answer
        print(msg.content)
        break

    for call in msg.tool_calls:          # ACT: run each tool the model asked for
        args = json.loads(call.function.arguments)
        result = get_weather(**args)
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": result,
        })
    # loop again so the model can use the tool result to answer
```

What happens when you run it:
1. The model sees the question and your tool, and **asks to call** `get_weather(city="Paris")`.
2. Your code runs the function and sends back `"It's 22°C and sunny in Paris."`
3. The model uses that result to write the final answer.

That's a real agent — it observed, decided to act, used a tool, and finished. Add more
functions (search, calculator, save-to-file…) and you can build agents that do real work.

> hermes-router prefers models confirmed to support function calling. If no candidate's
> capability can be confirmed, it tries the pool rather than rejecting the request early.

---

## Best practices (from the people who build agents for a living)

Anthropic's research on [building effective agents](https://www.anthropic.com/research/building-effective-agents)
boils down to one theme: **keep it simple**.

- **Start with the simplest thing that works.** Don't reach for a big framework on day one —
  a plain loop like the one above is often all you need.
- **Give it few, clear tools.** 2–4 well-described tools beat a dozen vague ones. Write good
  `description`s — the model relies on them to choose correctly.
- **Define when to stop.** Always have a clear end condition (here: "no more tool calls") so
  the loop can't run forever. A safety cap (e.g. max 10 iterations) is wise.
- **Add complexity only when it pays off.** Memory across sessions, vector search (RAG),
  multiple agents — add these when a real need appears, not before.
- **Keep the context tight.** Send what the model needs, not everything. Long histories cost
  tokens and can dilute focus (hermes-router caches and the host app can summarize).

## Where to go next

- **Long-term memory / search your own docs:** use embeddings (`/v1/embeddings`) to store and
  retrieve facts — the RAG pattern (see *embedding* and *RAG* in **[concepts.md](concepts.md)**).
- **Frameworks (optional):** once you understand the loop, tools like LangChain can save
  boilerplate — but you now know what they're doing under the hood.
- **More usage patterns:** **[usage.md](usage.md)** (Anthropic SDK, embeddings, streaming).

## Sources

- [Building Effective AI Agents — Anthropic](https://www.anthropic.com/research/building-effective-agents)
- [A Complete Beginner's Guide to Building AI Agents (2026) — Vellum](https://www.vellum.ai/blog/beginners-guide-to-building-ai-agents)
- [What is an agentic loop? — Make](https://www.make.com/en/blog/agentic-loop)

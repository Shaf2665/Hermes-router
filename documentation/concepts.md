# Concepts — a plain-language glossary

Every term you'll bump into while using hermes-router or building an agent, explained
simply. No prior AI experience assumed.

### LLM (Large Language Model)
The AI "brain." You give it text, it predicts and writes back text. GPT, Gemini, Claude,
and Llama are all LLMs. It has no memory of its own — every request is fresh (see
*context* and *memory* below).

### Provider
A company that runs LLMs you can reach over the internet — Gemini, Groq, Mistral, etc.
hermes-router juggles many of them so you don't depend on any single one.

### API key
A secret password that proves your program is allowed to use a provider. You get these
**free** from each provider. hermes-router stores them and rotates between them. (Don't
confuse a *provider key* with the router's own password, `PROXY_API_KEYS`.)

### Token
The unit LLMs read and write in — roughly ¾ of an English word (so ~100 tokens ≈ 75 words).
Limits, speed, and cost are all counted in tokens. "`max_tokens=200`" means "write at most
~150 words."

### Prompt
The text you send the model.

### System prompt
A special instruction at the very start that sets the model's behavior or persona — e.g.
"You are a helpful assistant that answers in one sentence." It's sent with every request.

### Context window
The maximum amount of text (in tokens) a model can consider at once — its short-term
attention span. Long conversations eventually exceed it, which is why agents summarize or
trim old messages.

### Streaming
Getting the reply word-by-word as it's generated (like watching someone type), instead of
waiting for the whole thing. Turn it on with `stream=True`.

### Embedding
A list of numbers that represents the *meaning* of a piece of text. Similar meanings get
similar numbers, which lets you search by meaning ("find notes about my trip") instead of
exact words. The basis of long-term memory and search. hermes-router serves these at
`/v1/embeddings`.

### Tool / function calling
Giving the model the ability to *do* things, not just talk. You describe some functions
(e.g. `get_weather(city)`); the model can choose to "call" one, you run it, and you hand the
result back. This is how an AI checks live data, does math, or controls other software.

### Agent
A program that uses an LLM to **pursue a goal over multiple steps** — thinking, using tools,
and reacting to results — rather than answering a single question. A chatbot replies once;
an agent keeps going until the job is done. (Build one in **[build-an-agent.md](build-an-agent.md)**.)

### Agent loop
The repeating cycle at the heart of every agent: **Observe** (look at the goal + what's
happened so far) → **Think** (decide the next step) → **Act** (run a tool or reply) → repeat
until done.

### Memory
How an agent remembers things, since the LLM itself doesn't.
- **Short-term:** keeping the recent conversation and re-sending it each turn.
- **Long-term:** storing facts (often as *embeddings*) and looking up the relevant ones
  later — across sessions.

### RAG (Retrieval-Augmented Generation)
A pattern where, before answering, you *retrieve* relevant text (via embeddings) and add it
to the prompt — so the model can answer using your documents, not just what it memorized.

### Rate limit
A cap providers put on how much you can use them for free in a given time. Hitting one is
why apps break — and exactly what hermes-router routes around by switching providers.

### Failover
Automatically switching to a backup when something fails. hermes-router fails over between
providers so one outage or rate limit can be absorbed while another candidate is available.

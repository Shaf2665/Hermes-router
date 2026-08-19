# Hall of Wisdom integration

Hermes Router is a multi-provider AI model router / inference gateway for Hall
of Wisdom. Hall owns all agent execution: task lifecycle, worktrees, file
editing, command execution, attachments, cancellation, and structured events.

```text
Hall of Wisdom
      ↓
Hall-owned Hermes adapter/runtime
      ↓
Hermes Router
      ↓
provider/model
```

Configure Hall with the router's OpenAI-compatible endpoint, client API key,
and model name (normally `hermes-router`). Hall's bundled Hermes adapter sends
ordinary `/v1/chat/completions` requests with function definitions. Hermes then
selects a capable provider/model, rotates keys, and fails over as needed.

The integration may use these optional, generic routing hints:

- `X-Hermes-Tool-Loop: true` requests strict tool-capable routing and disables
  response caching for an iterative tool loop.
- `X-Hermes-Session-Affinity: <opaque-id>` retains provider/model affinity
  across related inference turns while preserving failover.
- `X-Hermes-Workload-Hint: planning|coding|review|debug|vision` biases only
  candidate ordering using router capability metadata.

These headers do not create an agent API or expose filesystem, shell, task, or
event operations through Hermes. Hall remains responsible for all execution
policy and for never placing credentials in task or event payloads.

Hall owns the agent loop, task lifecycle, worktrees, file edits, commands, attachments,
cancellation, and execution events. Hermes owns inference routing, provider/model selection, key
rotation, failover, capability-aware routing, session affinity, and workload hints.

For normal clients, Hermes continues to support OpenAI-compatible chat
completions and Anthropic-compatible messages without Hall-specific setup.

---
title: "Hall of Wisdom"
description: "Use Hermes Router as Hall of Wisdom's multi-provider inference gateway."
---

Hermes Router is a multi-provider AI model router / inference gateway for Hall
of Wisdom. Hall owns agent execution: task lifecycle, worktrees, file editing,
commands, attachments, cancellation, and structured events.

```text
Hall of Wisdom
      ↓
Hall-owned Hermes adapter/runtime
      ↓
Hermes Router
      ↓
provider/model
```

Configure Hall with Hermes Router's OpenAI-compatible endpoint, client API key,
and model name. Hall's bundled adapter supplies tool definitions; Hermes routes
to the best available capable model and preserves normal key rotation and
provider failover.

The adapter can supply generic router hints for strict tool-loop transport,
session affinity, and workload-aware model selection. These never create an
agent-execution API in Hermes: filesystem access, commands, task control,
attachments, cancellation, and events remain Hall responsibilities.

Hall owns the agent loop, task lifecycle, worktrees, file edits, commands, attachments,
cancellation, and execution events. Hermes owns inference routing, provider/model selection, key
rotation, failover, capability-aware routing, session affinity, and workload hints.

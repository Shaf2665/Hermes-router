# Hall of Wisdom integration

Hermes Router now provides `hermes_hall_bridge.py`, a small, cross-platform
structured-CLI boundary for [Hall of Wisdom](https://github.com/Shaf2665/HallOfWisdom).
It lets Hall use the router's configured provider pool for **advisory work**:
planning, analysis, review, and implementation guidance.

This is intentionally not a replacement for Hall's Claude Code or Codex
adapters. Hermes Router is a model gateway; it does not itself own a coding
CLI tool loop. The bridge therefore makes no claim to read a project, edit a
file, run a command, or report file changes. Hall should route an editing task
to a verified coding-agent adapter and may use Hermes as a planner/reviewer.

## Why this boundary fits Hall

Hall's real-agent adapters launch a locally installed executable and keep
provider credentials out of `AgentTaskInput`, browser requests, normalized
events, and stored artifacts. The bridge preserves that design:

- Router URL, router key, model, and timeout are read only from the bridge
  process environment. They are never accepted as command-line arguments or
  in the prompt.
- Input is supplied only on stdin; no task-controlled text is parsed as a CLI
  option or a filesystem path.
- Stdout is JSONL only and contains bounded event data. Router error bodies,
  response headers, URLs, and keys are never forwarded.
- The bridge sends one ordinary, non-streaming OpenAI-compatible request with
  no tools. It cannot cause local side effects.

The bridge's configuration belongs in Hall's future local provider-connection
configuration, not in a task record or task metadata. Use a localhost router
unless an operator has explicitly chosen another protected endpoint.

## Local configuration

Start Hermes Router first, then make the following values available only to
the Hall Core process (or its dedicated adapter child environment):

```text
HERMES_ROUTER_BASE_URL=http://127.0.0.1:8319/v1
HERMES_ROUTER_API_KEY=<a PROXY_API_KEYS value>
HERMES_ROUTER_MODEL=hermes-router
HERMES_ROUTER_TIMEOUT_SECONDS=120
```

`HERMES_ROUTER_BASE_URL` must be an `http` or `https` URL ending exactly in
`/v1`. The key is mandatory and is deliberately not accepted through the
command line. The timeout is limited to 1–600 seconds.

Verify the bridge without sending a model request:

```bash
python hermes_hall_bridge.py detect
```

PowerShell uses the same command:

```powershell
python .\hermes_hall_bridge.py detect
```

It emits one safe JSON document. A successful result is:

```json
{"protocol":"hermes-hall-bridge/v1","available":true,"capabilities":["structured.events"]}
```

To exercise the event protocol directly:

```bash
printf 'Review the proposed migration and list risks.' | python hermes_hall_bridge.py run
```

## Contract for the Hall adapter phase

The next Hall of Wisdom phase should add a concrete
`@hall-of-wisdom/hermes-router-adapter` package and register it with Hall
Runner/Core. It should launch the bridge with structured process arguments:

```text
python <validated Hermes installation>/hermes_hall_bridge.py detect
python <validated Hermes installation>/hermes_hall_bridge.py run
```

For `run`, write the Hall-built advisory prompt to stdin and parse stdout as a
bounded JSONL stream. Do not use a shell, and do not pass the router key, URL,
or task text in argv. The adapter owns normalization through `EventFactory`:

| Bridge event | Hall mapping |
| --- | --- |
| `run.started` | `factory.runStarted()` |
| `message.delta` | `factory.messageDelta(text)` |
| `run.completed` | `factory.runCompleted(summary)` |
| `run.failed` | `factory.runFailed({ code, message })` |

The Hall descriptor should be `integrationLevel: "structured_cli"`, declare
`structured.events` (and `cancellation` only once the Hall run wrapper has
implemented it), and set `fileEditing`, `shellExecution`, `toolEvents`, and
`sessionResume` to `false`. Its detection result should only be `available`
after a successful `detect` document with the exact
`hermes-hall-bridge/v1` protocol. Child cancellation should be implemented by
Hall's existing bounded process-tree mechanism; this bridge itself has no tool
process to cancel.

Do not advertise `project.edit` or `command.execute` until a separate,
security-reviewed Hermes coding-agent tool-loop exists. Such a feature would
need an explicit permission model, path containment checks, process allowlists,
event mapping, cancellation, and isolated-worktree validation—the same class
of guarantees Hall already expects from its Codex and Claude Code adapters.

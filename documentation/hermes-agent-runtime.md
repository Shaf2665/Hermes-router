# Hermes Coding Runtime

Hermes Coding Runtime is a local, headless coding-agent process for Hall of
Wisdom. It uses Hermes Router for model inference while keeping file and command
execution out of the Flask server:

```text
Hall → local Hermes Coding Runtime → Hermes Router → provider/model
```

The runtime is `trusted_local`: it has strict worktree path containment and
bounded process execution, but it is not an operating-system sandbox and has no
command allowlist. Hall must launch it only in a Hall-owned worktree.

## CLI

From the Hermes repository or an environment where the package is importable:

```bash
python -m hermes_agent detect
python -m hermes_agent capabilities
python -m hermes_agent run
```

For a cross-platform adapter launching an installed/cloned Hermes directory,
use the file entrypoint. It preserves the caller's current working directory:

```text
python <hermes-directory>/hermes_agent_runner.py detect
python <hermes-directory>/hermes_agent_runner.py capabilities
python <hermes-directory>/hermes_agent_runner.py run
```

Linux/macOS installations can also use `hr agent detect|capabilities|run`.

The runtime reads the existing local integration settings:

```text
HERMES_ROUTER_BASE_URL=http://127.0.0.1:8319/v1
HERMES_ROUTER_API_KEY=<PROXY_API_KEYS value>
HERMES_ROUTER_MODEL=hermes-router
HERMES_ROUTER_TIMEOUT_SECONDS=120
```

Credentials are used only by the runtime's router client. They are removed from
every `command.execute` child environment and never appear in JSONL events.

## Detect and capabilities

`detect` verifies the configured router model and at least one available,
tool-capable provider model without making a model call:

```json
{"protocol":"hermes-agent/v1","runtime_version":"0.1.0","available":true,"capabilities":["project.read","project.edit","command.execute","structured.events","cancellation"],"integration_level":"structured_cli","execution_trust":"trusted_local"}
```

`capabilities` returns the static runtime contract without contacting the
router. `git.inspect` is deliberately not advertised: Git may be invoked through
the generic command tool, but this runtime does not provide a dedicated,
verified Git-inspection tool.

## Run input

`run` reads exactly one bounded UTF-8 JSON object from stdin:

```json
{"run_id":"hall-run-123","prompt":"Implement and test the requested change."}
```

- `prompt` is required.
- `run_id` is optional; the runtime generates one when omitted.
- Router URL, key, model, worktree path, command policy, and extra arguments are
  never accepted from task input.
- The process current working directory is the worktree root.

## Tool contract

The model receives only four tools:

- `project.read({path})` — reads one bounded UTF-8 file.
- `project.search({query, path?})` — literal bounded search.
- `project.apply_patch({path, old_text?, new_text, create?, expected_sha256?})`
  — replaces exactly one occurrence or creates a file; never deletes.
- `command.execute({argv, timeout_seconds?})` — structured argv, `shell=False`,
  fixed worktree cwd, bounded time/output, scrubbed environment, and process-tree
  cancellation.

All project paths are canonicalized beneath cwd. Absolute paths, traversal,
outside-resolving symlinks, and every `.git` path are rejected.

## JSONL protocol

Every run event includes:

```json
{"protocol":"hermes-agent/v1","runtime_version":"0.1.0","run_id":"hall-run-123","sequence":0,"type":"run.started","payload":{}}
```

Event types and payloads:

| Type | Payload |
| --- | --- |
| `run.started` | `{}` |
| `message.delta` | `{"text":"..."}` |
| `tool.started` | `{"tool_call_id":"...","tool_name":"..."}` |
| `tool.completed` | `{"tool_call_id":"...","tool_name":"...","success":true}` |
| `file.changed` | `{"path":"relative/path","operation":"created|modified"}` |
| `run.completed` | `{"summary":"..."}` |
| `run.failed` | `{"code":"...","message":"..."}` |
| `run.cancelled` | `{"cancelled_by":"orchestrator","reason":"..."}` |

Events are limited to 24 KB and sequences start at zero. File content, command
output, environment values, router response bodies, and credentials are not
included in events. Tool output is kept only in the transient model conversation.

SIGTERM or SIGINT cancels the loop, terminates the active command process tree,
and emits one `run.cancelled` event when the process can still write stdout.

## Agent routing profile

Runtime inference uses the existing `/v1/chat/completions` endpoint with two
internal routing headers:

```text
X-Hermes-Profile: agent
X-Hermes-Agent-Run: <run_id>
```

The profile does not expose any project tool over HTTP. It only changes routing:

- exact and semantic response caches are bypassed for reads and writes;
- a tool definition and a tool-capable candidate are mandatory;
- the last successful provider/model for a run is tried first on its next turn;
- the normal router provider/key/model failover loop remains authoritative, and
  a successful fallback becomes the new affinity target.

## Hall TypeScript adapter contract

The future Hall adapter needs only a local process boundary:

1. Resolve `python` and the absolute `hermes_agent_runner.py` path during
   adapter installation. Using `python -m hermes_agent` is equivalent when the
   Hermes repository is already importable.
2. For detection, run `detect` with the four `HERMES_ROUTER_*` variables above
   and select the adapter only when the JSON document has `available: true` and
   `protocol: "hermes-agent/v1"`.
3. For a task, spawn `run` with `cwd` set to Hall's prepared worktree. Write one
   JSON object containing Hall's bounded `run_id` and prompt, then close stdin.
4. Parse stdout one line at a time as JSON. Require the protocol, run id, and
   monotonically increasing sequence; map event `type` and `payload` directly.
   Treat stderr only as diagnostics and never parse it as the event stream.
5. Treat `run.completed`, `run.failed`, and `run.cancelled` as terminal. Exit 0
   accompanies completion or acknowledged cancellation; exit 1 accompanies a
   runtime failure. An unavailable `detect` document still exits 0, so inspect
   its `available` field.
6. To cancel, send SIGTERM to the runtime process and retain Hall's existing
   process-tree fallback. The runtime will terminate its active command tree and
   emit `run.cancelled` when stdout is still writable.

Hall continues to own worktree creation, cleanup, permissions, task policy, and
any higher-level approval UI. No session, memory, or worktree identifier should
be sent to Hermes beyond `run_id`, prompt, cwd, and the router environment.
